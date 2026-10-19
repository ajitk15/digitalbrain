"""Test workbench authorization, queue lifecycle and failure accounting."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import ApplicationGrant, ServiceOpsTestRun
from platform_core.serviceops_testing import process_next_test, queue_test

from . import test_documents


@override_settings(SERVICEOPS_TEST_AUTOMATION=True)
class TestWorkbenchTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("serviceops-testing", args=[self.app.pk])

    def test_owner_queues_closed_suite_and_duplicate_is_rejected(self):
        self.assertEqual(self.client.post(self.url, {"suite": "e2e"}).status_code, 302)
        self.client.post(self.url, {"suite": "e2e"})
        self.client.post(self.url, {"suite": "arbitrary-command"})
        self.assertEqual(ServiceOpsTestRun.objects.count(), 1)
        self.assertEqual(ServiceOpsTestRun.objects.get().status, "queued")

    def test_viewer_and_foreign_application_cannot_access_workbench(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"suite": "e2e"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("serviceops-testing",
                                                args=[self.other.pk])).status_code, 404)

    def test_worker_persists_results_and_report_is_application_scoped(self):
        run = queue_test(self.owner, self.app.pk, "e2e")
        with patch("platform_core.serviceops_testing.execute_test", return_value=(
                "passed", [{"test": "example", "status": "passed", "seconds": 1}], "Done")):
            self.assertTrue(process_next_test())
        run.refresh_from_db()
        self.assertEqual(run.status, "passed")
        report = reverse("serviceops-test-report", args=[self.app.pk, run.pk])
        self.assertEqual(self.client.get(report).json()["results"][0]["status"], "passed")
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.assertEqual(self.client.get(reverse("serviceops-test-report",
                                                args=[self.other.pk, run.pk])).status_code, 404)

    def test_revoking_owner_blocks_queued_execution(self):
        run = queue_test(self.owner, self.app.pk, "e2e")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        with patch("platform_core.serviceops_testing.subprocess.run") as launch:
            process_next_test()
        launch.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")

    def test_interrupted_worker_is_recovered(self):
        run = queue_test(self.owner, self.app.pk, "e2e")
        ServiceOpsTestRun.objects.filter(pk=run.pk).update(status="running",
            started_at=timezone.now() - timedelta(minutes=20))
        process_next_test()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")

    @override_settings(SERVICEOPS_TEST_AUTOMATION=False)
    def test_disabled_execution_is_not_queued(self):
        self.assertEqual(self.client.post(self.url, {"suite": "e2e"}).status_code, 403)
        self.assertFalse(ServiceOpsTestRun.objects.exists())

    def test_child_suite_has_its_own_config_and_no_live_credentials(self):
        import json
        import tomllib

        from platform_core.serviceops_testing import execute_test

        run = queue_test(self.owner, self.app.pk, "e2e")

        def launch(command, **kwargs):
            env = kwargs["env"]
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            config = tomllib.loads(Path(env["DIGITAL_BRAIN_CONFIG"]).read_text())
            self.assertEqual(config["mode"], "development")
            self.assertNotIn("database", config)
            self.assertEqual(
                config["secret_directory"], Path(env["SERVICEOPS_TEST_DIRECTORY"]).as_posix()
            )
            self.assertTrue((Path(config["secret_directory"]) / "django_secret_key").is_file())
            self.assertEqual(env["DIGITAL_BRAIN_BROWSER_CHANNEL"], "chromium")
            Path(env["SERVICEOPS_TEST_REPORT"]).write_text(json.dumps({
                "tests": [{"test": "closed-suite", "status": "passed"}],
                "successful": True, "count": 1,
            }))
            return SimpleNamespace(returncode=0)

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "must-not-reach-child",
                                      "DIGITAL_BRAIN_BROWSER_CHANNEL": "chromium"}),
            patch("platform_core.serviceops_testing.subprocess.run", side_effect=launch),
        ):
            self.assertEqual(execute_test(run)[0], "passed")
