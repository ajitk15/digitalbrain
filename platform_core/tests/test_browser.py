"""Browser end-to-end tests: real pages, real JavaScript, against a live server.

The rest of the suite checks rendered HTML and server behaviour. That is how a
script error that made every in-place progress update throw for a day went
unnoticed: no test ran the script. These drive the installed Chrome through
Playwright, so the JavaScript is exercised the way a reader's browser runs it.

The provider is never called: the model is replaced at `platform_core.ai`, and
because the live server runs in this process, the request threads and the
streaming worker thread see the same replacement.

Skipped, saying why, where Playwright or Chrome is not installed, or when
DIGITAL_BRAIN_BROWSER_TESTS=0. Run just these with:

    .venv/Scripts/python.exe manage.py test platform_core.tests.test_browser
"""

import os
import time
from types import SimpleNamespace
from unittest import SkipTest
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import (
    AIConfiguration,
    ApplicationFeature,
    ChatConversation,
    ChatMessage,
    GraphRevision,
    TriageRun,
)
from platform_core.workbench import add_knowledge

from . import test_documents

#: How long to wait for something the page should do by itself. Live progress
#: refreshes every 2.5 seconds, and one retry after a dropped connection waits 5.
PATIENCE_MS = 15000


def browser_or_skip():
    """(playwright, browser), or SkipTest with the reason."""
    if os.environ.get("DIGITAL_BRAIN_BROWSER_TESTS") == "0":
        raise SkipTest("Browser tests disabled by DIGITAL_BRAIN_BROWSER_TESTS=0.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SkipTest("Playwright is not installed: uv sync --group dev.") from None
    driver = sync_playwright().start()
    try:
        # The Chrome already on the machine: no separate browser download.
        browser = driver.chromium.launch(channel="chrome", headless=True)
    except Exception as failure:
        driver.stop()
        raise SkipTest(f"Chrome could not be started for browser tests: {failure}") from None
    return driver, browser


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class BrowserTestCase(StaticLiveServerTestCase):
    """A signed-in owner in a fresh page, with every script error collected."""

    @classmethod
    def setUpClass(cls):
        # Playwright's sync API runs an event loop on this thread; the ORM calls
        # the tests make here are ordinary synchronous ones.
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        cls.driver, cls.browser = browser_or_skip()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.sign_in(self.owner)
        self.page = self.context.new_page()
        self.page.set_default_timeout(PATIENCE_MS)
        self.script_errors = []
        self.page.on("pageerror", lambda error: self.script_errors.append(str(error)))
        self.page.on(
            "console",
            lambda message: (
                self.script_errors.append(message.text)
                if message.type == "error" and "favicon" not in message.text
                else None
            ),
        )

    def sign_in(self, user):
        """The session a sign-in would make, handed to the browser as its cookie."""
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        self.context.clear_cookies()
        self.context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                }
            ]
        )

    def open(self, name, *args, query=""):
        url = f"{self.live_server_url}{reverse(name, args=args)}{query}"
        self.page.goto(url)
        return url

    def assertNoScriptErrors(self):
        self.assertEqual(self.script_errors, [], "the page raised script or CSP errors")

    def mark_page(self):
        """A flag only this page load carries: a reload would lose it."""
        self.page.evaluate("window.__samePage = true")

    def assertSamePage(self):
        self.assertTrue(self.page.evaluate("window.__samePage === true"), "the page reloaded")


class PagesTests(BrowserTestCase):
    def test_every_main_screen_loads_without_script_or_csp_errors(self):
        """A thrown script or a refused inline style shows up here first."""
        for name in (
            "graph",
            "documents",
            "chat",
            "serviceops",
            "serviceops-runs",
            "onboarding",
            "plans",
            "code-graph",
            "connectors",
            "credentials",
            "application-features",
        ):
            with self.subTest(screen=name):
                self.script_errors.clear()
                self.open(name, self.app.pk)
                self.page.wait_for_load_state("networkidle")
                self.assertNoScriptErrors()


class ChatTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        add_knowledge(
            self.owner, self.app.pk, "Deploy runbook", "Traffic is served by carepath-api-green."
        )
        for purpose in ("chat", "graph_retrieval"):
            AIConfiguration.objects.create(
                application=self.app,
                purpose=purpose,
                provider="openai",
                model="gpt-5.6-luna",
                enabled=True,
                input_rate=1,
                output_rate=2,
                configured_by=self.owner,
            )
        for number in (1, 2):
            GraphRevision.objects.create(
                application=self.app,
                number=number,
                fingerprint=f"f{number}",
                published_at=timezone.now(),
                data={"nodes": [], "edges": [], "sources": []},
                quality={},
            )
        self.asked = []

    def provider(self):
        """The model, replaced: records what it was asked and streams a reply."""
        config = SimpleNamespace(provider="openai", model="gpt-5.6-luna")
        asked = self.asked

        def citations(app_id, question, version=None):
            asked.append({"version": version})
            return []

        def stream(user, app, config, token, question, seed, history, session):
            for piece in ("carepath-api-", "green serves traffic."):
                session.emit("delta", {"t": piece})
                time.sleep(0.05)
            return "carepath-api-green serves traffic.", []

        return (
            patch("platform_core.ai.chat_configuration", return_value=(self.app, config, "k")),
            patch("platform_core.ai.stream_chat_answer", side_effect=stream),
            patch("platform_core.ai.generate_title", return_value=None),
            patch("platform_core.graph_ai.graph_citations", side_effect=citations),
        )

    def ask(self, question):
        composer = self.page.locator("#chat-composer textarea")
        composer.fill(question)
        # Enter sends, as the composer says; the keyboard path is the one tested.
        composer.press("Enter")

    def test_a_new_conversation_answers_from_the_version_chosen(self):
        """The version picker sits outside the composer and joins it with
        form=. The streaming request once left it out, and every answer came
        from the published graph instead of the version chosen."""
        patches = self.provider()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.open("chat", self.app.pk, query="?new=1")
        self.page.select_option("#chat-mode", "graph")
        self.page.select_option("#graph-version", "1")
        self.mark_page()
        self.ask("Which deployment serves traffic?")
        answer = self.page.locator(".chat-assistant").last
        answer.get_by_text("carepath-api-green serves traffic.").wait_for()
        self.assertSamePage()
        conversation = ChatConversation.objects.get()
        self.assertEqual(conversation.graph_version, 1)
        self.assertEqual(self.asked, [{"version": 1}])
        self.assertEqual(ChatMessage.objects.get(role="assistant").status, "complete")
        self.assertNoScriptErrors()

    def test_a_lost_response_is_not_asked_twice(self):
        """The first request reaches the server, which saves it, but the browser
        never hears back. The fallback post carries the same key, and the server
        shows the saved conversation rather than asking - and paying - again."""
        patches = self.provider()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        dropped = []

        def lose_the_response(route):
            if route.request.method == "POST" and not dropped:
                route.fetch()  # the server saves the question...
                dropped.append(route.request.url)
                route.abort("connectionreset")  # ...and the browser never learns it
            else:
                route.continue_()

        self.page.route(
            f"{self.live_server_url}{reverse('chat', args=[self.app.pk])}**", lose_the_response
        )
        self.open("chat", self.app.pk, query="?new=1")
        self.ask("Which deployment serves traffic?")
        self.page.wait_for_url("**conversation=**")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(ChatMessage.objects.filter(role="user").count(), 1)
        conversation = ChatConversation.objects.get()
        self.assertIn(str(conversation.pk), self.page.url)
        self.assertEqual(ChatMessage.objects.filter(role="user").first().conversation, conversation)
        # The fallback's own navigation aborted the first fetch; that is the
        # scenario, not an error in the page's scripts.
        self.script_errors = [e for e in self.script_errors if "net::ERR" not in e]
        self.assertNoScriptErrors()


class LiveProgressTests(BrowserTestCase):
    """A run page follows the run by itself, and says so when it cannot."""

    def setUp(self):
        super().setUp()
        from platform_core.serviceops_triage import queue_run

        self.incident = add_knowledge(
            self.owner,
            self.app.pk,
            "Export times out",
            "Export times out\n\nType: Incident\nNumber: INC1\nState: New\n\n504 gateway timeout.",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )
        # No triage model is configured, so the run skips the model step and
        # nothing is ever charged.
        self.run = queue_run(self.owner, self.app.pk, self.incident)

    def open_run(self):
        self.open("serviceops", self.app.pk, query=f"?incident={self.incident.pk}")
        self.section = self.page.locator('[data-live="triage-run"]')
        self.assertEqual(self.section.get_attribute("data-live-pending"), "")
        self.mark_page()

    def finished(self):
        # A selector, not wait_for_function: that evaluates a string, which the
        # page's CSP refuses, as it should.
        self.page.locator('[data-live="triage-run"]:not([data-live-pending])').wait_for()

    def finish_run(self):
        from platform_core.serviceops_triage import execute_run

        execute_run(TriageRun.objects.get(pk=self.run.pk))

    def test_the_page_updates_in_place_when_the_run_finishes(self):
        self.open_run()
        self.finish_run()
        self.finished()
        self.assertSamePage()
        self.assertIn("Run #1", self.section.inner_text())
        self.assertNoScriptErrors()

    def test_a_dropped_connection_is_said_and_then_recovered_from(self):
        page_url = f"{self.live_server_url}{reverse('serviceops', args=[self.app.pk])}**"
        failures = []

        def drop_one_refresh(route):
            if route.request.resource_type == "fetch" and not failures:
                failures.append(route.request.url)
                route.abort("connectionreset")
            else:
                route.continue_()

        self.open_run()
        self.page.route(page_url, drop_one_refresh)
        status = self.page.locator("#live-status")
        status.wait_for()
        self.assertIn("Connection interrupted", status.inner_text())
        self.finish_run()
        self.finished()
        self.assertEqual(status.count(), 0, "the interruption notice outlived the recovery")
        self.assertSamePage()
        self.script_errors = [e for e in self.script_errors if "net::ERR" not in e]
        self.assertNoScriptErrors()


class OnboardingTests(BrowserTestCase):
    def test_the_connector_question_opens_as_a_dialog_and_saves(self):
        """modal.js lifts the form into a <dialog>; saving lands back on the list."""
        self.open("onboarding", self.app.pk)
        self.page.locator(
            f'a[href="{reverse("onboarding-connectors", args=[self.app.pk])}"]'
        ).first.click()
        dialog = self.page.locator("dialog[open]")
        dialog.get_by_text("Which systems does this application import from?").wait_for()
        dialog.locator('input[name="connector_github"]').uncheck()
        dialog.locator('input[name="connector_jira"]').check()
        dialog.get_by_role("button", name="Save").click()
        self.page.get_by_text("Using Jira").first.wait_for()
        rows = dict(
            ApplicationFeature.objects.filter(
                application=self.app, key__startswith="connector_"
            ).values_list("key", "enabled")
        )
        self.assertEqual(
            rows,
            {"connector_github": False, "connector_jira": True, "connector_servicenow": True},
        )
        self.assertNoScriptErrors()

    def test_choosing_operations_hides_the_approval_box_without_script(self):
        """Hidden by CSS :has() alone - and the server still records the grant."""
        self.open("application-create", self.app.organization_id)
        approval = self.page.locator(".engineering-only")
        self.page.locator('input[name="purpose"][value="engineering"]').check()
        self.assertTrue(approval.is_visible())
        self.page.locator('input[name="purpose"][value="operations"]').check()
        self.assertFalse(approval.is_visible())
        self.page.locator('input[name="purpose"][value="both"]').check()
        self.assertTrue(approval.is_visible())
        self.assertNoScriptErrors()
