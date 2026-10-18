"""The worker can run apart from the web process, and nothing then runs twice.

Phase 0 of the scale work: `serve.py --no-worker` serves requests only,
`manage.py run_worker` runs the lanes, each lane is held by one process at a
time, a stop is honoured between steps, scanning can go through a clamd daemon,
and `queue_status` says what each lane has waiting and whether anything is
running it.
"""

import io
import json
import runpy
import struct
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from digitalbrain.configuration import load_config
from platform_core import clamd, document_worker, processing
from platform_core.document_worker import (
    LANES,
    LaneKeeper,
    lane_key,
    lanes_named,
    run_lane,
)
from platform_core.models import (
    Application,
    Document,
    KnowledgeGraph,
    LaneHeartbeat,
    Organization,
    Portfolio,
    Product,
    User,
)
from platform_core.queue_status import STALE_AFTER, lane_status

ROOT = Path(__file__).resolve().parents[2]


class LaneSelectionTests(SimpleTestCase):
    def test_lanes_are_returned_in_the_order_asked_and_once(self):
        chosen = lanes_named(["graph", "intake", "graph"])
        self.assertEqual([name for name, _, _ in chosen], ["graph", "intake"])

    def test_an_unknown_lane_is_refused_by_name(self):
        with self.assertRaises(ValueError) as raised:
            lanes_named(["intake", "nonsense"])
        self.assertIn("nonsense", str(raised.exception))

    def test_every_lane_has_its_own_stable_lock_number(self):
        keys = [lane_key(name) for name, _, _ in LANES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(lane_key("intake"), lane_key("intake"))
        # pg_try_advisory_lock takes a signed 64-bit integer.
        self.assertTrue(all(-(2**63) <= key < 2**63 for key in keys))


class StopTests(SimpleTestCase):
    def test_a_stop_is_honoured_between_steps_and_the_lane_is_released(self):
        stop = threading.Event()
        calls = []

        def first():
            calls.append("first")
            stop.set()
            return True

        def second():
            calls.append("second")
            return True

        keeper = MagicMock()
        keeper.ready.return_value = True
        run_lane("intake", lambda: (first, second), 0, stop, keeper)
        # The step that was running finished; the one after it never started.
        self.assertEqual(calls, ["first"])
        keeper.release.assert_called_once()

    def test_a_lane_this_process_does_not_hold_runs_nothing(self):
        stop = threading.Event()
        ran = []
        keeper = MagicMock()
        keeper.ready.side_effect = lambda: stop.set() or False
        run_lane("intake", lambda: (lambda: ran.append(1),), 0, stop, keeper)
        self.assertEqual(ran, [])

    def test_an_idle_lane_wakes_for_a_stop_instead_of_sleeping_on(self):
        stop = threading.Event()
        thread = threading.Thread(
            target=run_lane, args=("maintenance", lambda: (lambda: False,), 60, stop, None)
        )
        thread.start()
        stop.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "a 60 s idle wait ignored the stop")


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.owner.statements.append(sql)
        if self.owner.broken and sql == "SELECT 1":
            raise OSError("connection lost")

    def fetchone(self):
        return (self.owner.granted,)


class FakeConnection:
    def __init__(self, granted=True):
        self.granted = granted
        self.broken = False
        self.closed = False
        self.statements = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@patch.object(document_worker.LaneKeeper, "beat")
class PostgresLaneLockTests(SimpleTestCase):
    """The PostgreSQL path, against a stand-in connection: CI runs on SQLite."""

    def keeper(self, connection):
        keeper = LaneKeeper("intake")
        vendor = patch.object(document_worker.connection, "vendor", "postgresql")
        create = patch.object(
            document_worker.connections, "create_connection", return_value=connection
        )
        vendor.start()
        create.start()
        self.addCleanup(vendor.stop)
        self.addCleanup(create.stop)
        return keeper

    def test_the_first_process_takes_the_lane_on_a_connection_of_its_own(self, beat):
        held = FakeConnection(granted=True)
        keeper = self.keeper(held)
        self.assertTrue(keeper.ready())
        self.assertIn("SELECT pg_try_advisory_lock(%s)", held.statements)
        self.assertFalse(held.closed, "closing it would release the lock")
        beat.assert_called_once()

    def test_a_second_process_stands_by_and_keeps_nothing_open(self, beat):
        refused = FakeConnection(granted=False)
        keeper = self.keeper(refused)
        self.assertFalse(keeper.ready())
        self.assertTrue(refused.closed)
        beat.assert_not_called()

    def test_a_lost_lock_connection_gives_the_lane_up(self, beat):
        held = FakeConnection(granted=True)
        keeper = self.keeper(held)
        self.assertTrue(keeper.ready())
        held.broken = True
        keeper.checked = 0.0
        with patch.object(document_worker, "logger") as logger:
            self.assertFalse(keeper.ready())
        self.assertEqual(
            logger.warning.call_args.kwargs["extra"]["event"], "worker_lane_lock_failed"
        )

    def test_a_deliberate_stop_unlocks_for_the_standby(self, beat):
        held = FakeConnection(granted=True)
        keeper = self.keeper(held)
        keeper.ready()
        keeper.release()
        self.assertIn("SELECT pg_advisory_unlock(%s)", held.statements)
        self.assertTrue(held.closed)
        self.assertFalse(keeper.holding)


class SqliteLaneTests(TestCase):
    def test_sqlite_has_no_advisory_locks_so_the_keeper_holds_and_beats(self):
        keeper = LaneKeeper("graph")
        self.assertTrue(keeper.ready())
        beat = LaneHeartbeat.objects.get(lane="graph")
        self.assertTrue(beat.owner)
        self.assertLess(timezone.now() - beat.beat_at, timedelta(seconds=5))


class RunWorkerCommandTests(SimpleTestCase):
    def test_unknown_lanes_are_refused(self):
        with self.assertRaises(CommandError):
            call_command("run_worker", lanes="intake,bogus", stdout=io.StringIO())

    def test_it_runs_the_lanes_asked_for_and_stops_on_request(self):
        started = {}

        def fake_start(lanes, stop):
            started["names"] = [name for name, _, _ in lanes]
            stop.set()
            return []

        output = io.StringIO()
        with (
            patch("platform_core.management.commands.run_worker.start_lanes", fake_start),
            patch("platform_core.management.commands.run_worker.signal.signal") as handler,
        ):
            call_command("run_worker", lanes="intake,graph", stdout=output)
        self.assertEqual(started["names"], ["intake", "graph"])
        self.assertEqual(handler.call_count, 2, "SIGTERM and SIGINT both stop it")
        self.assertIn("worker: stopped", output.getvalue())

    def test_without_lanes_it_runs_every_lane(self):
        started = {}

        def fake_start(lanes, stop):
            started["names"] = [name for name, _, _ in lanes]
            stop.set()
            return []

        with (
            patch("platform_core.management.commands.run_worker.start_lanes", fake_start),
            patch("platform_core.management.commands.run_worker.signal.signal"),
        ):
            call_command("run_worker", stdout=io.StringIO())
        self.assertEqual(started["names"], [name for name, _, _ in LANES])


class ServeFlagTests(SimpleTestCase):
    """serve.py starts the lanes unless --no-worker says another process runs them."""

    def serve(self, *flags):
        threads = []

        class RecordingThread:
            def __init__(self, target=None, **kwargs):
                threads.append(target)

            def start(self):
                pass

        with (
            patch("waitress.serve") as serve,
            patch("threading.Thread", RecordingThread),
            patch("sys.argv", ["serve.py", "--instance", "test", *flags]),
        ):
            runpy.run_path(str(ROOT / "scripts/serve.py"), run_name="__main__")
        serve.assert_called_once()
        return threads

    def test_by_default_the_worker_runs_in_the_web_process(self):
        self.assertIn(document_worker.run_document_worker, self.serve())

    def test_no_worker_serves_requests_only(self):
        self.assertNotIn(document_worker.run_document_worker, self.serve("--no-worker"))


def version_reply(when):
    return f"ClamAV 1.4.1/27431/{when:%a %b %d %H:%M:%S %Y}".encode() + b"\0"


class FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        reply, self.reply = self.reply[:size], self.reply[size:]
        return reply

    def close(self):
        pass


@override_settings(CONFIG={"clamd": "clamd:3310"})
class ClamdTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "upload.quarantine"
        self.path.write_bytes(b"x" * 70000)

    def scan(self, verdict, built=None):
        built = built or datetime.now(UTC) - timedelta(days=1)
        sockets = [FakeSocket(version_reply(built)), FakeSocket(verdict)]
        with patch.object(clamd, "connect", side_effect=sockets):
            return clamd.scan(self.path), sockets

    def test_a_clean_file_is_zero_and_is_sent_framed(self):
        result, sockets = self.scan(b"stream: OK\0")
        self.assertEqual(result, 0)
        sent = sockets[1].sent
        self.assertTrue(sent.startswith(b"zINSTREAM\0"))
        self.assertTrue(sent.endswith(struct.pack("!L", 0)), "the stream must be terminated")
        first = struct.unpack("!L", sent[10:14])[0]
        self.assertEqual(first, clamd.CHUNK)
        self.assertEqual(len(sent), 10 + 4 + clamd.CHUNK + 4 + (70000 - clamd.CHUNK) + 4)

    def test_a_match_is_one(self):
        result, _ = self.scan(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
        self.assertEqual(result, 1)

    def test_signatures_more_than_a_week_old_fail_closed(self):
        with self.assertRaises(clamd.ScanUnavailable):
            self.scan(b"stream: OK\0", built=datetime.now(UTC) - timedelta(days=8))

    def test_a_daemon_with_no_database_date_fails_closed(self):
        with patch.object(clamd, "connect", return_value=FakeSocket(b"ClamAV 1.4.1\0")):
            with self.assertRaises(clamd.ScanUnavailable):
                clamd.scan(self.path)

    def test_an_oversized_stream_fails_closed(self):
        with self.assertRaises(clamd.ScanUnavailable):
            self.scan(b"INSTREAM size limit exceeded. ERROR\0")

    def test_an_unreachable_daemon_fails_closed(self):
        with patch.object(clamd, "connect", side_effect=ConnectionRefusedError):
            with self.assertRaises(clamd.ScanUnavailable):
                clamd.scan(self.path)

    def test_addresses(self):
        self.assertEqual(clamd.target("clamd:3310"), ("tcp", ("clamd", 3310)))
        self.assertEqual(clamd.target("/run/clamd.sock"), ("unix", "/run/clamd.sock"))
        with self.assertRaises(clamd.ScanUnavailable):
            clamd.target("clamd")


@override_settings(DOCUMENT_SCAN_REQUIRED=True, CONFIG={"clamd": "clamd:3310", "scanner": ""})
class ProcessingThroughClamdTests(SimpleTestCase):
    def test_scanning_is_ready_with_a_daemon_and_no_clamscan(self):
        self.assertTrue(processing.scanning_ready())

    def test_the_daemon_scans_instead_of_a_clamscan_process(self):
        with (
            patch.object(clamd, "scan", return_value=1) as scan,
            patch("platform_core.processing.subprocess.run") as run,
        ):
            self.assertEqual(processing.scan_file(Path("upload.quarantine")), 1)
        scan.assert_called_once()
        run.assert_not_called()

    def test_a_daemon_that_cannot_scan_reads_as_unscanned(self):
        with patch.object(clamd, "scan", side_effect=clamd.ScanUnavailable("reply")):
            self.assertEqual(processing.scan_file(Path("upload.quarantine")), 2)

    @override_settings(CONFIG={"scanner": ""})
    def test_without_a_daemon_nothing_has_changed(self):
        self.assertFalse(processing.scanning_ready())


class ClamdConfigurationTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_host_and_port_or_a_socket_path_are_accepted(self):
        self.assertEqual(self.load('clamd = "clamd:3310"\n')["clamd"], "clamd:3310")
        self.assertEqual(self.load('clamd = "/run/clamd.sock"\n')["clamd"], "/run/clamd.sock")

    def test_anything_else_is_refused(self):
        for value in ('"clamd"', '"relative/clamd.sock"', '"clamd:99999"', "3310"):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                self.load(f"clamd = {value}\n")


class QueueStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner")
        org = Organization.objects.create(name="Example")
        portfolio = Portfolio.objects.create(name="P", organization=org)
        product = Product.objects.create(name="Product", portfolio=portfolio)
        self.app = Application.objects.create(name="Primary", product=product)

    def document(self, status):
        return Document.objects.create(
            application=self.app,
            uploaded_by=self.user,
            name="a.md",
            size=1,
            sha256="0" * 64,
            status=status,
            origin="upload",
        )

    def rows(self):
        return {row["lane"]: row for row in lane_status()}

    def test_waiting_work_is_counted_from_the_rows_the_lane_would_take(self):
        self.document("queued")
        self.document("pending")
        self.document("ready")
        KnowledgeGraph.objects.create(application=self.app, status="queued")
        rows = self.rows()
        self.assertEqual(rows["intake"]["waiting"], 2)
        self.assertIsNotNone(rows["intake"]["oldest_seconds"])
        self.assertEqual(rows["graph"]["waiting"], 1)
        self.assertEqual(rows["factory"]["waiting"], 0)
        self.assertIsNone(rows["factory"]["oldest_seconds"])

    def test_a_quiet_heartbeat_is_stale_and_a_fresh_one_is_not(self):
        now = timezone.now()
        LaneHeartbeat.objects.create(lane="intake", owner="w:1", started_at=now, beat_at=now)
        LaneHeartbeat.objects.create(
            lane="graph", owner="w:1", started_at=now, beat_at=now - STALE_AFTER * 2
        )
        rows = self.rows()
        self.assertFalse(rows["intake"]["stale"])
        self.assertTrue(rows["graph"]["stale"])
        self.assertTrue(rows["factory"]["stale"], "a lane nobody holds is stale")

    def test_the_command_check_fails_for_a_lane_with_no_worker(self):
        now = timezone.now()
        LaneHeartbeat.objects.create(lane="intake", owner="w:1", started_at=now, beat_at=now)
        call_command("queue_status", check=True, lanes="intake", stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("queue_status", check=True, lanes="intake,graph", stdout=io.StringIO())

    def test_json_output_carries_figures_and_nothing_about_records(self):
        self.document("queued")
        output = io.StringIO()
        call_command("queue_status", json=True, stdout=output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), len(LANES))
        self.assertEqual(
            set(rows[0]),
            {"lane", "waiting", "oldest_seconds", "owner", "heartbeat_seconds", "stale"},
        )
        self.assertNotIn("a.md", output.getvalue())

    def test_the_log_line_is_kept_by_the_allowlist_formatter(self):
        import logging

        from platform_core.observability import SafeJsonFormatter

        record = logging.LogRecord("q", logging.INFO, "", 0, "queue status", None, None)
        for key, value in {
            "event": "queue_status",
            "lane": "intake",
            "waiting": 3,
            "oldest_seconds": 40,
            "heartbeat_seconds": 5,
            "stale": False,
        }.items():
            setattr(record, key, value)
        entry = json.loads(SafeJsonFormatter().format(record))
        self.assertEqual(entry["waiting"], 3)
        self.assertEqual(entry["oldest_seconds"], 40)
        self.assertIs(entry["stale"], False)
