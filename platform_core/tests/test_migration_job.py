"""Migrations are applied once, by one process, however many containers start.

Phase 5: `manage.py apply_migrations` holds a PostgreSQL advisory lock for the
whole run, lifts the request statement timeout for its own session, and tries a
migration again only when a table lock was busy. The scale overlay runs it as a
one-shot job that the web and worker containers wait for, which is what lets
the web container be more than one replica.
"""

import contextlib
import importlib.util
import io
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections
from django.test import SimpleTestCase, TestCase

from platform_core.document_worker import LANES, lane_key
from platform_core.management.commands import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
COMMAND = "platform_core.management.commands.apply_migrations"


class Busy(Exception):
    """What psycopg raises when lock_timeout gives up."""

    sqlstate = "55P03"


class Other(Exception):
    sqlstate = "42P01"


def failing(cause, times):
    """A stand-in for `migrate` that fails `times` times with `cause`, then works."""
    calls = []

    def migrate(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) <= times:
            try:
                raise cause()
            except cause as failure:
                raise OperationalError(str(failure)) from failure

    return migrate, calls


@patch(f"{COMMAND}.time.sleep")
class RetryTests(SimpleTestCase):
    def test_a_busy_table_is_tried_again_and_then_applied(self, sleep):
        migrate, calls = failing(Busy, 2)
        said = []
        with patch(f"{COMMAND}.call_command", migrate):
            attempt = apply_migrations.migrate(said.append)
        self.assertEqual(attempt, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(said), 2)
        self.assertTrue(all(call["interactive"] is False for call in calls))

    def test_any_other_failure_is_raised_at_once(self, sleep):
        migrate, calls = failing(Other, 1)
        with patch(f"{COMMAND}.call_command", migrate), self.assertRaises(OperationalError):
            apply_migrations.migrate(lambda line: None)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_a_table_that_stays_busy_fails_the_deploy(self, sleep):
        migrate, calls = failing(Busy, 99)
        with patch(f"{COMMAND}.call_command", migrate), self.assertRaises(OperationalError):
            apply_migrations.migrate(lambda line: None)
        self.assertEqual(len(calls), apply_migrations.LOCK_ATTEMPTS)

    def test_the_lock_number_is_its_own(self, sleep):
        self.assertNotIn(apply_migrations.KEY, [lane_key(name) for name, _, _ in LANES])
        self.assertTrue(-(2**63) <= apply_migrations.KEY < 2**63)


class CommandTests(TestCase):
    def run_command(self, **options):
        out = io.StringIO()
        migrate, calls = failing(Busy, 0)
        with patch(f"{COMMAND}.call_command", migrate), patch(f"{COMMAND}.time.sleep"):
            call_command("apply_migrations", stdout=out, **options)
        return calls, out.getvalue()

    def test_it_applies_migrations(self):
        calls, _ = self.run_command()
        self.assertEqual(len(calls), 1)

    def test_the_lock_is_taken_and_given_back(self):
        if connection.vendor != "postgresql":
            self.skipTest("Advisory locks exist only on PostgreSQL; SQLite just migrates.")
        held = []

        def migrate(*args, **kwargs):
            other = connections.create_connection("default")
            try:
                with other.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [apply_migrations.KEY])
                    held.append(not cursor.fetchone()[0])
                with connection.cursor() as cursor:
                    cursor.execute("SHOW statement_timeout")
                    held.append(cursor.fetchone()[0])
            finally:
                other.close()

        with patch(f"{COMMAND}.call_command", migrate):
            call_command("apply_migrations", stdout=io.StringIO())
        self.assertEqual(held, [True, "0"])
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [apply_migrations.KEY])
                self.assertTrue(cursor.fetchone()[0], "the lock was not given back")
                cursor.execute("SELECT pg_advisory_unlock(%s)", [apply_migrations.KEY])
        finally:
            other.close()

    def test_a_second_caller_waits_and_says_so_then_gives_up(self):
        if connection.vendor != "postgresql":
            self.skipTest("Advisory locks exist only on PostgreSQL; SQLite just migrates.")
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [apply_migrations.KEY])
            out = io.StringIO()
            migrate, calls = failing(Busy, 0)
            with (
                patch(f"{COMMAND}.call_command", migrate),
                self.assertRaises(CommandError) as refused,
            ):
                call_command("apply_migrations", wait=0, stdout=out)
            self.assertEqual(calls, [])
            self.assertIn("waiting for it", out.getvalue())
            self.assertIn("over 0 seconds", str(refused.exception))
        finally:
            other.close()


#: Migrations up to this one were written before web replicas could overlap a
#: deploy, and are not held to the rule below.
LAST_UNCHECKED = 90
#: A migration that does break the previous release, on purpose - the second
#: half of an expand/contract pair, a release after the code stopped using what
#: it removes. Name it here with the reason.
CONTRACTS = {}


def breaking(operation):
    """Why the previous release could not run against this operation, or None."""
    from django.db import migrations
    from django.db.models import NOT_PROVIDED

    if isinstance(operation, migrations.RemoveField | migrations.DeleteModel):
        return "removes what the previous release still reads"
    if isinstance(operation, migrations.RenameField | migrations.RenameModel):
        return "renames what the previous release still names"
    if isinstance(operation, migrations.AddField):
        field = operation.field
        # A Python `default` fills existing rows and is then dropped from the
        # column, so the previous release's inserts, which omit it, would fail.
        # Only a database default or a nullable column survives them.
        if not field.many_to_many and not field.null and field.db_default is NOT_PROVIDED:
            return "adds a required column the previous release does not write"
    return None


class ExpandContractTests(TestCase):
    """Every new migration is one the release before it can still run against.

    The migrate job puts the new schema in place while the old web containers
    are still serving, so a column they do not write must not be required, and
    nothing they read may be removed or renamed in the same release.
    """

    def test_no_new_migration_breaks_the_release_before_it(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        found = []
        for (app, name), migration in loader.disk_migrations.items():
            number = name.split("_", 1)[0]
            if app != "platform_core" or not number.isdigit() or int(number) <= LAST_UNCHECKED:
                continue
            for operation in migration.operations:
                reason = breaking(operation)
                if reason and name not in CONTRACTS:
                    found.append(f"{name}: {operation.describe()} {reason}")
        self.assertEqual(found, [], "add it to CONTRACTS only as the second half of a pair")

    def test_the_rule_recognises_what_it_is_for(self):
        from django.db import migrations, models

        self.assertIsNotNone(breaking(migrations.RemoveField("job", "lane")))
        self.assertIsNotNone(breaking(migrations.RenameField("job", "lane", "queue")))
        self.assertIsNotNone(breaking(migrations.AddField("job", "tier", models.CharField(
            max_length=8, default="a"))))
        self.assertIsNone(breaking(migrations.AddField("job", "tier", models.CharField(
            max_length=8, null=True))))
        self.assertIsNone(breaking(migrations.AddField("job", "tier", models.CharField(
            max_length=8, db_default="a"))))


def service(text, name):
    """One top-level service's block of a compose file, as text."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n)+)", text, re.M)
    if match is None:
        raise AssertionError(f"no {name} service")
    return match.group(1)


class DeploymentTests(SimpleTestCase):
    """The files that run outside Django say what the command expects them to."""

    def setUp(self):
        self.entrypoint = (ROOT / "deploy/entrypoint.sh").read_text(encoding="utf-8")
        self.scale = (ROOT / "docker-compose.scale.yaml").read_text(encoding="utf-8")

    def test_the_entrypoint_applies_migrations_only_under_the_lock(self):
        self.assertIn("python manage.py apply_migrations", self.entrypoint)
        self.assertNotRegex(self.entrypoint, r"manage\.py migrate --noinput")
        self.assertIn("all|web|worker|migrate)", self.entrypoint)

    def test_the_signing_key_is_linked_into_place_so_two_writers_keep_one(self):
        self.assertIn('ln "/run/secrets/.django_secret_key.$$" /run/secrets/django_secret_key',
                      self.entrypoint)
        self.assertNotIn("> /run/secrets/django_secret_key", self.entrypoint)

    def test_the_entrypoint_keeps_unix_line_endings(self):
        for name in ("deploy/entrypoint.sh", "docker-compose.yaml", "docker-compose.scale.yaml",
                     "docker-compose.observability.yaml",
                     "docker-compose.observability.scale.yaml", "docker-compose.dev.yaml"):
            self.assertNotIn(b"\r", (ROOT / name).read_bytes(), name)

    def test_the_migrate_job_runs_once_and_everything_else_waits_for_it(self):
        job = service(self.scale, "migrate")
        self.assertIn('DIGITAL_BRAIN_ROLE: "migrate"', job)
        self.assertIn('restart: "no"', job)
        for name in ("app", "worker", "sandbox"):
            block = service(self.scale, name)
            self.assertRegex(
                block, r"migrate:\n\s+condition: service_completed_successfully", name
            )

    def test_every_lane_runs_in_exactly_one_of_the_worker_and_the_sandbox(self):
        from platform_core.document_worker import LANES

        def lanes(name):
            found = re.search(r'DIGITAL_BRAIN_LANES: "([^"]+)"', service(self.scale, name))
            return found[1].split(",")

        worker, sandbox = lanes("worker"), lanes("sandbox")
        self.assertEqual(set(worker) & set(sandbox), set())
        self.assertEqual(sorted(worker + sandbox), sorted(name for name, _, _ in LANES))
        # The lanes that give outside content to another program.
        self.assertEqual(set(sandbox), {"intake", "code-graph", "factory"})

    def test_the_sandbox_cannot_write_its_image_or_gain_a_privilege(self):
        sandbox = service(self.scale, "sandbox")
        for line in (
            "read_only: true",
            "cap_drop:\n      - ALL",
            '"no-new-privileges:true"',
            "pids_limit: ",
            "mem_limit: ",
            "- /tmp:",
            "runtime-secrets:/run/secrets:ro",
            "managed-credentials:/var/lib/digitalbrain/credentials:ro",
        ):
            self.assertIn(line, sandbox)
        self.assertNotIn("ports:", sandbox)

    def test_each_service_is_healthy_on_its_own_lanes(self):
        for name in ("worker", "sandbox"):
            self.assertIn('queue_status --check --lanes \\"$$DIGITAL_BRAIN_LANES\\"',
                          service(self.scale, name), name)

    def test_the_web_container_waits_and_may_be_replicated(self):
        app = service(self.scale, "app")
        self.assertIn('DIGITAL_BRAIN_MIGRATIONS: "wait"', app)
        self.assertIn("replicas: ${DIGITAL_BRAIN_WEB_REPLICAS:-1}", app)

    def test_only_secrets_init_and_the_migrate_job_can_read_the_owner_password(self):
        base = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        split = {
            name: {**mounts(base, name), **mounts(self.scale, name)}
            for name in sorted(set(services(base)) | set(services(self.scale)))
        }
        self.assertEqual(readers(split), {"secrets-init", "postgres", "migrate"})
        # One container doing everything also applies the migrations, as the owner.
        alone = {name: mounts(base, name) for name in services(base)}
        self.assertEqual(readers(alone), {"secrets-init", "postgres", "app"})

    def test_web_worker_and_sandbox_read_the_runtime_set_and_only_web_writes_it(self):
        for name, writable in (("app", True), ("worker", False), ("sandbox", False)):
            self.assertEqual(
                mounts(self.scale, name)["/run/secrets"], ("runtime-secrets", writable), name
            )
        self.assertEqual(
            mounts(self.scale, "migrate")["/run/runtime-secrets"], ("runtime-secrets", True)
        )
        self.assertIn(
            'DIGITAL_BRAIN_RUNTIME_SECRETS: "/run/runtime-secrets"', service(self.scale, "migrate")
        )
        self.assertIn("\n  runtime-secrets:", self.scale.split("\nvolumes:\n", 1)[1])

    def test_the_migrate_job_writes_the_runtime_set_last(self):
        job = self.entrypoint[self.entrypoint.index('if [ "$ROLE" = "migrate" ]; then'):]
        self.assertLess(
            job.index('python /app/deploy/runtime_secrets.py "$CONFIG" "$RUNTIME_SECRETS"'),
            job.index("exit 0"),
        )
        self.assertIn("only the migrate job writes DIGITAL_BRAIN_RUNTIME_SECRETS", self.entrypoint)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("chmod 700 /run/secrets /run/runtime-secrets", dockerfile)


#: The volumes that hold the owner's database password: the store, and
#: Postgres's own copy of it.
OWNER_PASSWORD_VOLUMES = {"platform-secrets", "database-secret"}


def services(text):
    """The top-level service names of a compose file."""
    body = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    return re.findall(r"^  ([a-z][a-z0-9-]*):\n", body, re.M)


def mounts(text, name):
    """One service's named-volume mounts, {target: (volume, writable)}.

    Keyed on the target because that is how compose merges an overlay's
    volumes into the base file's: the same target is replaced.
    """
    if name not in services(text):
        return {}
    found = re.search(r"^    volumes:\n((?:      .*\n|\s*\n)+)", service(text, name), re.M)
    if found is None:
        return {}
    return {
        target: (volume, flag != ":ro")
        for volume, target, flag in re.findall(
            r"^      - ([a-z][a-z0-9-]*):(/[^:\s]*)(:ro)?$", found[1], re.M
        )
    }


def readers(services):
    """Which services mount a volume holding the owner's database password."""
    return {
        name
        for name, mounted in services.items()
        if any(volume in OWNER_PASSWORD_VOLUMES for volume, _ in mounted.values())
    }


def runtime_secrets():
    """deploy/runtime_secrets.py, which is a script and not a package module."""
    spec = importlib.util.spec_from_file_location(
        "runtime_secrets", ROOT / "deploy/runtime_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeSecretTests(SimpleTestCase):
    """The set every container but the migrate job reads, as the job writes it."""

    CLAUDE = "claude_00000000-0000-0000-0000-000000000001"

    def setUp(self):
        self.module = runtime_secrets()
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.store = Path(work.name) / "store"
        self.target = Path(work.name) / "runtime"
        self.store.mkdir()
        self.target.mkdir()
        for name, value in (
            ("django_secret_key", "value-signing"),
            ("database_password", "value-owner"),
            ("database_runtime_password", "value-runtime"),
            ("redis_password", "value-redis"),
            (self.CLAUDE, "value-sk-ant"),
            (".django_secret_key.42", "partial"),
        ):
            self.put(self.store / name, value)
        (self.store / "..data").mkdir()

    def put(self, path, value, mtime=None):
        path.write_text(value, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def held(self):
        return {path.name: path.read_text(encoding="utf-8") for path in self.target.iterdir()}

    def write(self, password="database_runtime_password"):
        return self.module.write(self.store, self.target, password)

    def test_the_runtime_set_never_holds_the_owner_password(self):
        self.write()
        self.assertEqual(self.held(), {
            "django_secret_key": "value-signing",
            "database_runtime_password": "value-runtime",
            "redis_password": "value-redis",
            self.CLAUDE: "value-sk-ant",
        })

    def test_connecting_as_the_owner_gives_the_owner_password_instead(self):
        self.assertEqual(self.module.runtime_password({"database": {}}), "database_password")
        self.write()
        changed = self.write("database_password")
        self.assertEqual(changed["removed"], ["database_runtime_password"])
        self.assertEqual(self.held()["database_password"], "value-owner")
        self.assertNotIn("database_runtime_password", self.held())
        # Turning the runtime role back on takes the owner's away again.
        self.write()
        self.assertNotIn("database_password", self.held())

    def test_a_credential_mounted_since_is_kept_but_a_newer_store_copy_wins(self):
        self.put(self.store / self.CLAUDE, "old", mtime=1_000)
        self.put(self.target / self.CLAUDE, "mounted since", mtime=2_000)
        self.write()
        self.assertEqual(self.held()[self.CLAUDE], "mounted since")
        self.put(self.store / self.CLAUDE, "rotated", mtime=3_000)
        self.write()
        self.assertEqual(self.held()[self.CLAUDE], "rotated")

    def test_what_the_platform_generates_always_comes_from_the_store(self):
        for name in ("django_secret_key", "database_runtime_password", "redis_password"):
            self.put(self.target / name, "edited here", mtime=9_000_000_000)
        self.write()
        self.assertEqual(self.held()["django_secret_key"], "value-signing")
        self.assertEqual(self.held()["database_runtime_password"], "value-runtime")
        self.assertEqual(self.held()["redis_password"], "value-redis")

    def test_the_previous_signing_key_follows_the_store_both_ways(self):
        """Rotating the signing key keeps the old one as a fallback for a while.

        An old key left in the runtime set after the store dropped it would go
        on accepting sessions signed with it.
        """
        self.put(self.store / "django_secret_key_previous", "value-old")
        self.put(self.target / "django_secret_key_previous", "edited here", mtime=9_000_000_000)
        self.write()
        self.assertEqual(self.held()["django_secret_key_previous"], "value-old")
        (self.store / "django_secret_key_previous").unlink()
        changed = self.write()
        self.assertEqual(changed["removed"], ["django_secret_key_previous"])
        self.assertNotIn("django_secret_key_previous", self.held())

    def test_a_missing_password_or_an_unknown_one_stops_the_job(self):
        (self.store / "database_runtime_password").unlink()
        with self.assertRaises(SystemExit):
            self.write()
        with self.assertRaises(SystemExit):
            self.module.runtime_password({"database": {"password_secret": "jira_x"}})

    def test_every_file_is_owner_only(self):
        if os.name == "nt":
            self.skipTest("Permission bits are POSIX; read_secret checks them only there.")
        self.write()
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        for path in self.target.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_it_reads_the_configuration_and_prints_no_value(self):
        from digitalbrain.configuration import DATABASE_PASSWORDS

        self.assertEqual(self.module.DATABASE_PASSWORDS, DATABASE_PASSWORDS)
        config = self.store.parent / "production.toml"
        config.write_text(
            f'secret_directory = "{self.store.as_posix()}"\n[database]\n'
            'password_secret = "database_runtime_password"\n',
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.module.main([str(config), str(self.target)])
        self.assertNotIn("database_password", self.held())
        self.assertNotIn("value-", out.getvalue())
        self.assertIn("never database_password", out.getvalue())
