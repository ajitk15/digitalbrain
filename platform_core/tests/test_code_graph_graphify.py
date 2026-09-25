import socket
from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core.code_factory import reference_files
from platform_core.code_graph_analysis import facts
from platform_core.code_graph_graphify import analyse
from platform_core.code_graph_ingest import index_repository, register
from platform_core.models import CodeRelationship, FactoryRun

from . import test_code_factory_delivery as delivery
from .test_documents import DocumentTests

JAVA = [
    (
        "src/main/java/com/acme/OrderService.java",
        "package com.acme;\n\npublic class OrderService {\n"
        "    private final PaymentClient payments = new PaymentClient();\n\n"
        "    public boolean place(String id) {\n"
        "        return payments.charge(id, 10);\n    }\n}\n",
    ),
    (
        "src/main/java/com/acme/PaymentClient.java",
        "package com.acme;\n\npublic class PaymentClient extends BaseClient {\n"
        "    public boolean charge(String id, int amount) {\n        return send(id);\n    }\n}\n",
    ),
    (
        "src/main/java/com/acme/BaseClient.java",
        "package com.acme;\n\npublic class BaseClient {\n"
        "    protected boolean send(String id) { return true; }\n}\n",
    ),
]
GO = [
    ("svc/main.go", "package main\n\nfunc main() {\n\tServe()\n}\n"),
    ("svc/server.go", "package main\n\nfunc Serve() {\n\thandle()\n}\n\nfunc handle() {}\n"),
]


def analysed(files):
    return [facts(path, text) for path, text in files]


def key(edge):
    """(source file name, target file name, kind) - enough to name an edge here."""
    return (edge["source"].rsplit("/", 1)[-1], edge["target"].rsplit("/", 1)[-1], edge["kind"])


def edges_of(edges):
    return {key(edge) for edge in edges}


class GraphifyAdapterTests(TestCase):
    def test_java_and_go_gain_symbols_and_a_cross_file_call_graph(self):
        """The in-house analyser reads neither; Graphify reads both."""
        symbols, edges, failed = analyse(analysed(JAVA + GO))
        self.assertEqual(failed, set())
        service = symbols["src/main/java/com/acme/OrderService.java"]
        self.assertIn({"name": "OrderService", "line": 3, "kind": "class"}, service)
        self.assertIn({"name": "place", "line": 6, "kind": "function"}, service)
        self.assertIn(
            {"name": "Serve", "line": 3, "kind": "function"}, symbols["svc/server.go"]
        )
        found = edges_of(edges)
        # Same package, so no import joins these: only the call graph does.
        self.assertIn(("OrderService.java", "PaymentClient.java", "call"), found)
        self.assertIn(("PaymentClient.java", "BaseClient.java", "call"), found)
        self.assertIn(("PaymentClient.java", "BaseClient.java", "inherit"), found)
        self.assertIn(("main.go", "server.go", "call"), found)

    def test_each_edge_says_where_it_was_read_and_how_sure_it_is(self):
        _, edges, _ = analyse(analysed(JAVA))
        by_key = {key(edge): edge for edge in edges}
        inherit = by_key[("PaymentClient.java", "BaseClient.java", "inherit")]
        self.assertEqual(inherit["confidence"], "static")  # written in the source
        self.assertEqual(inherit["evidence"]["lines"], [3])
        call = by_key[("OrderService.java", "PaymentClient.java", "call")]
        self.assertEqual(call["confidence"], "inferred")  # resolved across files
        self.assertEqual(call["evidence"]["names"], ["charge"])
        self.assertEqual(call["evidence"]["lines"], [7])

    def test_python_keeps_its_own_symbols_and_imports_and_gains_calls(self):
        files = analysed(
            [
                ("app/main.py", "from app.util import helper\n\n\ndef run():\n    helper()\n"),
                ("app/util.py", "def helper():\n    pass\n"),
            ]
        )
        symbols, edges, _ = analyse(files)
        self.assertNotIn("app/main.py", symbols)  # ast already read these
        found = edges_of(edges)
        self.assertIn(("main.py", "util.py", "call"), found)
        self.assertNotIn(("main.py", "util.py", "import"), found)

    def test_it_runs_offline(self):
        """Only the AST pass is used; it must never reach the network."""

        def refuse(*args, **kwargs):
            raise AssertionError("Graphify opened a network connection")

        with patch.object(socket.socket, "connect", refuse):
            _, edges, _ = analyse(analysed(GO))
        self.assertIn(("main.go", "server.go", "call"), edges_of(edges))

    def test_a_path_that_would_leave_the_scratch_directory_is_never_written(self):
        files = analysed(GO) + [
            {**facts("svc/evil.go", "package main\n"), "path": "../evil.go"}
        ]
        symbols, edges, _ = analyse(files)
        self.assertNotIn("../evil.go", symbols)
        self.assertTrue(all(".." not in edge["source"] for edge in edges))


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphifyIndexingTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)

    def _index(self, clone, files):
        clone.return_value = ("c" * 40, "main", list(files), [], True)
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        return repository, repository.snapshots.get()

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_a_java_snapshot_stores_symbols_and_call_edges(self, clone, _token):
        repository, snapshot = self._index(clone, JAVA)
        self.assertEqual(repository.status, "ready")
        self.assertEqual(snapshot.analyzer_version, "structural-v5-graphify")
        service = snapshot.files.get(path="src/main/java/com/acme/OrderService.java")
        self.assertEqual(service.language, "java")
        self.assertIn("place", [item["name"] for item in service.symbols])
        kinds = set(
            CodeRelationship.objects.filter(snapshot=snapshot).values_list(
                "source__path", "target__path", "kind"
            )
        )
        self.assertIn(
            (
                "src/main/java/com/acme/OrderService.java",
                "src/main/java/com/acme/PaymentClient.java",
                "call",
            ),
            kinds,
        )
        # Linked files are not orphans any more; before, every Java file was.
        self.assertNotEqual(service.role, "orphan")

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    @patch("platform_core.code_graph_ingest.graphify_analyse", side_effect=RuntimeError)
    def test_a_graphify_failure_keeps_the_snapshot_and_says_so(self, _graphify, clone, _token):
        repository, snapshot = self._index(
            clone,
            [
                ("app/main.py", "from app import util\n"),
                ("app/util.py", "def helper():\n    pass\n"),
            ],
        )
        self.assertEqual(repository.status, "partial")
        self.assertFalse(snapshot.complete)
        self.assertTrue(any("Call graph" in warning for warning in snapshot.warnings))
        # What the in-house analyser found is still there.
        self.assertTrue(
            CodeRelationship.objects.filter(snapshot=snapshot, kind="import").exists()
        )


@override_settings(**delivery.SETTINGS)
class GraphifyCodeFactoryTests(TestCase):
    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_code_factory_is_shown_what_a_java_target_calls(self, clone, _token):
        """Imports alone reach nothing in one Java package; calls reach the client."""
        clone.return_value = ("c" * 40, "main", list(JAVA), [], True)
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        FactoryRun.objects.filter(pk=self.run.pk).update(
            code_snapshot=repository.snapshots.get()
        )
        self.run.refresh_from_db()
        chosen = reference_files(
            self.run, [{"path": "src/main/java/com/acme/OrderService.java"}]
        )
        self.assertIn(
            "src/main/java/com/acme/PaymentClient.java", [item["path"] for item in chosen]
        )
