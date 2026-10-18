"""The platform runs as a role that can touch rows and nothing else.

Phase 5: the migrate step runs as the owner and creates (or corrects) the
runtime role; everything else connects as that role. These tests make the role
on the test server, sign in as it, and check what it can and cannot do.
"""

import base64
import io
import secrets
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from digitalbrain.configuration import load_config
from platform_core import database_roles


class VerifierTests(SimpleTestCase):
    def test_the_verifier_is_what_postgresql_stores_and_carries_no_password(self):
        salt = b"0123456789abcdef"
        verifier = database_roles.scram_verifier("pencil-and-paper", salt=salt)
        method, rest = verifier.split("$", 1)
        self.assertEqual(method, "SCRAM-SHA-256")
        header, keys = rest.split("$")
        iterations, encoded_salt = header.split(":")
        self.assertEqual(int(iterations), database_roles.SCRAM_ITERATIONS)
        self.assertEqual(base64.b64decode(encoded_salt), salt)
        stored, server = (base64.b64decode(part) for part in keys.split(":"))
        self.assertEqual((len(stored), len(server)), (32, 32))
        self.assertNotIn("pencil", verifier)
        self.assertEqual(verifier, database_roles.scram_verifier("pencil-and-paper", salt=salt))
        self.assertNotEqual(verifier, database_roles.scram_verifier("pencil-and-paper"))

    def test_a_name_outside_the_shape_is_refused_not_quoted(self):
        for name in ["", "Runtime", "a b", 'x"; DROP ROLE app; --', "9role", "r" * 64, None]:
            self.assertFalse(database_roles.valid(name), name)
            with self.assertRaises(ValueError):
                database_roles.ensure(name, "password")
        self.assertTrue(database_roles.valid("digital_brain_runtime"))


class RuntimeRoleTests(SimpleTestCase):
    """Against the test server, committed: a role must exist to sign in as it."""

    databases = {"default"}

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Roles are PostgreSQL's; SQLite has none.")
        self.role = f"test_runtime_{uuid.uuid4().hex[:12]}"
        self.password = secrets.token_urlsafe(24)
        self.addCleanup(self.drop)

    def drop(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [self.role])
            if cursor.fetchone():
                cursor.execute(f'DROP OWNED BY "{self.role}"')
                cursor.execute(f'DROP ROLE "{self.role}"')

    def sign_in(self, password=None):
        import psycopg

        settings = connection.settings_dict
        return psycopg.connect(
            host=settings["HOST"],
            port=settings["PORT"],
            dbname=settings["NAME"],
            user=self.role,
            password=password or self.password,
            autocommit=True,
        )

    def attributes(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolinherit, "
                "rolcanlogin FROM pg_roles WHERE rolname = %s",
                [self.role],
            )
            return cursor.fetchone()

    def test_it_signs_in_and_reads_and_writes_rows(self):
        import psycopg

        database_roles.ensure(self.role, self.password)
        self.assertEqual(self.attributes(), (False, False, False, False, False, True))
        with self.sign_in() as runtime:
            self.assertEqual(runtime.execute("SELECT current_user").fetchone()[0], self.role)
            runtime.execute("SELECT count(*) FROM platform_core_job").fetchone()
            with runtime.transaction(force_rollback=True):
                runtime.execute(
                    "INSERT INTO platform_core_laneheartbeat (lane, owner, started_at, beat_at) "
                    "VALUES ('probe', 'probe', now(), now())"
                )
                runtime.execute("DELETE FROM platform_core_laneheartbeat WHERE lane = 'probe'")
            # An advisory lock is all the lanes and the migrate step need.
            self.assertTrue(runtime.execute("SELECT pg_try_advisory_lock(42)").fetchone()[0])
        with self.assertRaises(psycopg.OperationalError):
            self.sign_in(password="not-the-password")

    def test_it_cannot_change_the_schema_or_become_the_owner(self):
        import psycopg

        database_roles.ensure(self.role, self.password)
        owner = connection.settings_dict["USER"]
        with self.sign_in() as runtime:
            for statement in [
                "CREATE TABLE probe (id int)",
                "DROP TABLE platform_core_job",
                "ALTER TABLE platform_core_job ADD COLUMN probe int",
                "TRUNCATE platform_core_job",
                f'SET ROLE "{owner}"',
                "CREATE ROLE probe_role",
                "COPY platform_core_job TO PROGRAM 'true'",
            ]:
                with self.subTest(statement), self.assertRaises(psycopg.Error):
                    runtime.execute(statement)

    def test_running_it_again_undoes_what_was_added_by_hand(self):
        database_roles.ensure(self.role, self.password)
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER ROLE "{self.role}" CREATEDB CREATEROLE BYPASSRLS')
        self.assertEqual(self.attributes()[1:4], (True, True, True))
        rotated = secrets.token_urlsafe(24)
        database_roles.ensure(self.role, rotated)
        self.assertEqual(self.attributes(), (False, False, False, False, False, True))
        # Replacing the file and deploying again is how the password rotates.
        self.password = rotated
        with self.sign_in() as runtime:
            self.assertEqual(runtime.execute("SELECT 1").fetchone()[0], 1)

    def test_the_owner_itself_is_refused(self):
        with self.assertRaises(ValueError):
            database_roles.ensure(connection.settings_dict["USER"], self.password)


class ConfigurationTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "{}"\n[database]\n'

    def load(self, database):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                self.BASE.format(Path(directory).as_posix())
                + 'name = "db"\nhost = "127.0.0.1"\n' + database,
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_the_runtime_role_and_its_password_file_are_accepted(self):
        self.load('user = "owner"\nruntime_user = "digital_brain_runtime"\n')
        self.load('user = "digital_brain_runtime"\npassword_secret = "database_runtime_password"\n')

    def test_a_runtime_role_that_is_not_a_plain_name_or_is_the_owner_is_refused(self):
        for value in ['"Runtime"', '"x; DROP"', '"owner"', "7"]:
            with self.subTest(value), self.assertRaises(ImproperlyConfigured):
                self.load(f'user = "owner"\nruntime_user = {value}\n')

    def test_the_password_comes_only_from_one_of_two_named_files(self):
        for value in ['"django_secret_key"', '"../database_password"', '"/etc/passwd"']:
            with self.subTest(value), self.assertRaises(ImproperlyConfigured):
                self.load(f'user = "owner"\npassword_secret = {value}\n')


class MigrateStepTests(TestCase):
    """The migrate step makes the runtime role, under its lock, after migrating."""

    def run_command(self):
        with (
            patch("platform_core.management.commands.apply_migrations.call_command"),
            patch.object(database_roles, "ensure") as ensure,
            patch("digitalbrain.configuration.read_secret", return_value="runtime-secret"),
        ):
            call_command("apply_migrations", stdout=io.StringIO())
        return ensure

    def test_it_creates_the_role_the_configuration_names(self):
        if connection.vendor != "postgresql":
            self.skipTest("Roles are PostgreSQL's; on SQLite the step only migrates.")
        with override_settings(DATABASE_RUNTIME_USER="digital_brain_runtime"):
            ensure = self.run_command()
        ensure.assert_called_once_with("digital_brain_runtime", "runtime-secret")

    def test_without_one_it_only_migrates(self):
        with override_settings(DATABASE_RUNTIME_USER=""):
            ensure = self.run_command()
        ensure.assert_not_called()