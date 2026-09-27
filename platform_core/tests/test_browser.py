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

import json
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
    """(playwright, browser), or SkipTest with the reason.

    With DIGITAL_BRAIN_REQUIRE_BROWSER=1 - set it in any CI job that is meant to
    run these - a missing Playwright or a browser that will not start is a
    failure, not a skip, so the coverage cannot quietly disappear.
    """
    required = os.environ.get("DIGITAL_BRAIN_REQUIRE_BROWSER") == "1"

    def unavailable(reason):
        if required:
            raise RuntimeError(f"Browser tests are required here, but {reason}")
        raise SkipTest(reason)

    if os.environ.get("DIGITAL_BRAIN_BROWSER_TESTS") == "0" and not required:
        raise SkipTest("Browser tests disabled by DIGITAL_BRAIN_BROWSER_TESTS=0.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        unavailable("Playwright is not installed: uv sync --group dev.")
    driver = sync_playwright().start()
    try:
        # The Chrome already on the machine: no separate browser download.
        browser = driver.chromium.launch(channel="chrome", headless=True)
    except Exception as failure:
        driver.stop()
        unavailable(f"Chrome could not be started for browser tests: {failure}")
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


#: Nothing a reader needs is set smaller than this. Measured as rendered, so a
#: rule in the legacy stylesheet that still wins the cascade is caught too.
MIN_TEXT_PX = 11

SMALL_TEXT = """(minimum) => {
  const small = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const text = walker.currentNode.textContent.trim();
    const el = walker.currentNode.parentElement;
    if (!text || !el || el.closest('svg, [aria-hidden="true"], .sr-only')) continue;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height || getComputedStyle(el).visibility === 'hidden') continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size >= minimum) continue;
    const tag = el.tagName.toLowerCase();
    small.push(`${size}px <${tag} class="${el.className}">${text.slice(0, 40)}`);
  }
  return [...new Set(small)];
}"""


class ReadabilityTests(BrowserTestCase):
    def test_no_screen_sets_text_smaller_than_the_minimum(self):
        for name in (
            "dashboard",
            "graph",
            "documents",
            "chat",
            "serviceops",
            "onboarding",
            "plans",
        ):
            with self.subTest(screen=name):
                if name == "dashboard":
                    self.page.goto(f"{self.live_server_url}{reverse('dashboard')}")
                else:
                    self.open(name, self.app.pk)
                self.page.wait_for_load_state("networkidle")
                self.assertEqual(self.page.evaluate(SMALL_TEXT, MIN_TEXT_PX), [])


#: What a screen reader or a keyboard needs from every page, checked as
#: rendered. Not a full WCAG audit - no contrast, no reading order - but the
#: failures here are the ones that make a control unusable rather than awkward.
ACCESSIBILITY = r"""() => {
  const found = [];
  const visible = (el) => {
    if (el.closest('[hidden], template, dialog:not([open])')) return false;
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };
  const describe = (el) => `<${el.tagName.toLowerCase()}${el.id ? ' id=' + el.id : ''}` +
    `${el.className && typeof el.className === 'string' ? ' class="' + el.className + '"' : ''}` +
    `${el.name ? ' name=' + el.name : ''}>`;
  const named = (el) => {
    if ((el.getAttribute('aria-label') || '').trim()) return true;
    const by = el.getAttribute('aria-labelledby');
    const text = (id) => (document.getElementById(id)?.textContent || '').trim();
    if (by && by.split(/\s+/).some(text)) {
      return true;
    }
    if ((el.getAttribute('title') || '').trim()) return true;
    return false;
  };
  if (!document.documentElement.lang) found.push('no lang on <html>');
  if (document.querySelectorAll('main').length !== 1) found.push('not exactly one <main>');
  if (!document.querySelector('h1')) found.push('no <h1>');
  const ids = {};
  for (const el of document.querySelectorAll('[id]')) ids[el.id] = (ids[el.id] || 0) + 1;
  for (const [id, count] of Object.entries(ids)) if (count > 1) found.push(`duplicate id ${id}`);
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (el.type === 'hidden' || !visible(el)) continue;
    const labelled = named(el) || [...(el.labels || [])].some((l) => l.textContent.trim());
    if (!labelled) found.push(`unlabelled ${describe(el)}`);
  }
  for (const el of document.querySelectorAll('button, a[href], [role="button"], summary')) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.textContent || '').trim();
    const img = [...el.querySelectorAll('img[alt]')].some((i) => i.alt.trim());
    if (!text && !named(el) && !img) found.push(`unnamed ${describe(el)}`);
  }
  for (const el of document.querySelectorAll('img')) {
    if (!el.hasAttribute('alt')) found.push(`img without alt ${el.src.slice(-40)}`);
  }
  for (const el of document.querySelectorAll('svg')) {
    if (el.closest('[aria-hidden="true"]') || el.getAttribute('aria-hidden') === 'true') continue;
    if (el.getAttribute('role') === 'img' && named(el)) continue;
    if (el.closest('a, button') && (el.closest('a, button').textContent || '').trim()) continue;
    found.push(`svg neither hidden nor named in ${describe(el.parentElement)}`);
  }
  let last = 0;
  const headings = document.querySelectorAll('main :is(h1, h2, h3, h4, h5, h6)');
  for (const h of headings) {
    if (!visible(h)) continue;
    const level = Number(h.tagName[1]);
    const title = h.textContent.trim().slice(0, 40);
    if (last && level > last + 1) found.push(`heading jumps h${last} to h${level}: ${title}`);
    last = level;
  }
  return [...new Set(found)];
}"""

#: The focused element, described, if nothing marks it: no outline and no
#: box-shadow, on it or - for a visually hidden radio - on its label.
FOCUS_MARK = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return '';
  const marked = (node) => {
    if (!node) return false;
    const style = getComputedStyle(node);
    return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
      || style.boxShadow !== 'none';
  };
  const label = el.labels && el.labels[0];
  if (marked(el) || marked(label) || marked(label && label.querySelector('span'))) return '';
  const text = (el.textContent || el.name || '').trim().slice(0, 30);
  return `<${el.tagName.toLowerCase()} class="${el.className}">${text}`;
}"""

#: WCAG 2.2 text contrast, computed as rendered: 4.5:1, or 3:1 for large text
#: (24px, or 18.66px bold). The background is found by walking up to the first
#: opaque colour, blending each translucent layer on the way, and an ancestor's
#: opacity fades the text toward it. Text over an image or a gradient is not
#: judged - there is no single background colour to judge it against - and
#: neither is a disabled control, which WCAG exempts.
CONTRAST = r"""() => {
  const parse = (value) => {
    const m = value.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const over = (top, under) => ({
    r: top.r * top.a + under.r * (1 - top.a),
    g: top.g * top.a + under.g * (1 - top.a),
    b: top.b * top.a + under.b * (1 - top.a), a: 1,
  });
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const background = (el) => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.backgroundImage !== 'none') return null;
      const colour = parse(style.backgroundColor);
      if (colour && colour.a > 0) { layers.push(colour); if (colour.a >= 1) break; }
    }
    let result = {r: 255, g: 255, b: 255, a: 1};
    for (const layer of layers.reverse()) result = over(layer, result);
    return result;
  };
  const found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const text = walker.currentNode.textContent.trim();
    const el = walker.currentNode.parentElement;
    const unseen = 'svg, [hidden], template, dialog:not([open]), .sr-only';
    if (!text || !el || el.closest(unseen)) continue;
    if (el.closest(':disabled, [aria-disabled="true"], option')) continue;
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!box.width || !box.height || style.visibility === 'hidden') continue;
    const bg = background(el);
    if (!bg) continue;
    let opacity = 1;
    for (let node = el; node; node = node.parentElement) {
      opacity *= Number(getComputedStyle(node).opacity);
    }
    const colour = parse(style.color);
    const fg = over({...colour, a: colour.a * opacity}, bg);
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) {
      found.push(`${ratio.toFixed(2)} ${style.color} on rgb(${bg.r|0},${bg.g|0},${bg.b|0}) ` +
        `<${el.tagName.toLowerCase()} class="${el.className}">${text.slice(0, 30)}`);
    }
  }
  return [...new Set(found)];
}"""

#: Every screen a person reaches from the navigation.
AUDITED = (
    "graph",
    "documents",
    "source-add",
    "chat",
    "serviceops",
    "serviceops-runs",
    "onboarding",
    "plans",
    "code-graph",
    "connectors",
    "credentials",
    "application-features",
    "ai-settings",
    "usage",
    "application-access",
    "api-tokens",
    "chat-settings",
)


class AccessibilityTests(BrowserTestCase):
    def test_every_screen_names_its_controls_and_keeps_its_headings_in_order(self):
        report = {}
        for name in ("dashboard", "attention", *AUDITED):
            if name in ("dashboard", "attention"):
                self.page.goto(f"{self.live_server_url}{reverse(name)}")
            else:
                self.open(name, self.app.pk)
            self.page.wait_for_load_state("networkidle")
            problems = self.page.evaluate(ACCESSIBILITY)
            if problems:
                report[name] = problems
        self.assertEqual(report, {}, json.dumps(report, indent=1))

    def test_text_has_enough_contrast_to_read(self):
        report = {}
        for name in ("dashboard", "attention", *AUDITED):
            if name in ("dashboard", "attention"):
                self.page.goto(f"{self.live_server_url}{reverse(name)}")
            else:
                self.open(name, self.app.pk)
            self.page.wait_for_load_state("networkidle")
            problems = self.page.evaluate(CONTRAST)
            if problems:
                report[name] = problems
        self.assertEqual(report, {}, json.dumps(report, indent=1))

    def test_every_control_reached_by_tab_shows_where_the_focus_is(self):
        unmarked = {}
        for name in ("documents", "source-add", "chat", "serviceops", "onboarding", "ai-settings"):
            self.open(name, self.app.pk)
            self.page.wait_for_load_state("networkidle")
            missing = []
            for _ in range(40):
                self.page.keyboard.press("Tab")
                found = self.page.evaluate(FOCUS_MARK)
                if found:
                    missing.append(found)
            if missing:
                unmarked[name] = sorted(set(missing))
        self.assertEqual(unmarked, {}, json.dumps(unmarked, indent=1))

    def test_a_popup_is_usable_from_the_keyboard_alone(self):
        self.open("documents", self.app.pk)
        opener = self.page.locator("a[data-modal][href$='/documents/add/']").first
        opener.focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("dialog[open] main, dialog[open] h1")
        inside = self.page.evaluate("!!document.activeElement.closest('dialog[open]')")
        self.assertTrue(inside, "focus stayed behind the popup")
        self.page.keyboard.press("Escape")
        self.assertFalse(self.page.evaluate("!!document.querySelector('dialog[open]')"))
        back = self.page.evaluate("document.activeElement.getAttribute('href') || ''")
        self.assertTrue(back.endswith("/documents/add/"), "focus did not return to its opener")
        self.assertNoScriptErrors()


class ChatLayoutTests(BrowserTestCase):
    """Chat's own task fits a laptop window: at 1280x720 the message area had
    100px and Send was below the fold, and a new chat opened on a disabled
    "AI · setup required" mode with Send enabled."""

    def setUp(self):
        super().setUp()
        from platform_core.models import Application

        Application.objects.filter(pk=self.app.pk).update(setup_completed_at=timezone.now())

    def measure(self):
        return self.page.evaluate(
            """() => {
              const box = (s) => document.querySelector(s).getBoundingClientRect();
              const mode = document.getElementById('chat-mode');
              return {
                send: box('#chat-send').bottom, composer: box('#chat-composer').top,
                messages: box('#chat-messages').height,
                starter: Math.max(...[...document.querySelectorAll('.chat-starters a')]
                  .map((a) => a.getBoundingClientRect().bottom)),
                starters: document.querySelectorAll('.chat-starters a').length,
                messagesBottom: box('#chat-messages').bottom,
                mode: mode.value, modeDisabled: mode.selectedOptions[0].disabled,
                height: innerHeight,
              };
            }"""
        )

    def test_the_conversation_and_send_fit_a_laptop_window(self):
        """Every starter, not just the first, and with setup unfinished too:
        the setup strip takes height, and that is when the composer covered
        the lower suggestions."""
        from platform_core.models import AIConfiguration, Application

        cases = [(finished, ai) for finished in (True, False) for ai in (False, True)]
        for finished, ai in cases:
            Application.objects.filter(pk=self.app.pk).update(
                setup_completed_at=timezone.now() if finished else None
            )
            AIConfiguration.objects.filter(application=self.app, purpose="chat").delete()
            if ai:
                # AI mode offers the longer synthesis questions.
                AIConfiguration.objects.create(
                    application=self.app,
                    purpose="chat",
                    provider="openai",
                    model="gpt-5.6-luna",
                    input_rate=1,
                    output_rate=1,
                    configured_by=self.owner,
                    enabled=True,
                )
            for width, height in ((1280, 720), (1366, 768)):
                with self.subTest(size=f"{width}x{height}", setup_finished=finished, ai=ai):
                    self.page.set_viewport_size({"width": width, "height": height})
                    self.open("chat", self.app.pk, query="?new=1")
                    found = self.measure()
                    self.assertGreater(found["starters"], 1)
                    self.assertLessEqual(found["send"], found["height"], "Send is below the fold")
                    self.assertGreaterEqual(found["messages"], 200)
                    self.assertLessEqual(found["starter"], found["composer"], "starter covered")
        self.assertNoScriptErrors()

    def test_without_ai_a_new_chat_opens_on_a_mode_that_works(self):
        self.open("chat", self.app.pk, query="?new=1")
        found = self.measure()
        self.assertEqual(found["mode"], "search")
        self.assertFalse(found["modeDisabled"])
        self.assertIn("AI answers are not set up here", self.page.inner_text(".chat-setup"))


class SettingsTabsTests(BrowserTestCase):
    """The settings tabs wrapped to three rows (89px) at 1280px, so the
    navigation pushed every settings screen down before it said anything."""

    def test_the_settings_tabs_fit_one_row_on_a_laptop(self):
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.open("ai-settings", self.app.pk)
        found = self.page.evaluate(
            """() => {
              const tabs = document.querySelector('.settings-tabs');
              const links = [...tabs.querySelectorAll('a')];
              const top = (a) => Math.round(a.getBoundingClientRect().top);
              return {links: links.length, tops: [...new Set(links.map(top))]};
            }"""
        )
        self.assertGreaterEqual(found["links"], 6)
        self.assertEqual(len(found["tops"]), 1, found)
        strip = self.page.evaluate(
            "(() => { const t = document.querySelector('.settings-tabs');"
            " return [t.scrollWidth, t.clientWidth]; })()"
        )
        self.assertLessEqual(strip[0], strip[1], "the strip scrolls at 1280px")
        self.assertNoScriptErrors()

    def test_a_narrow_window_scrolls_the_tabs_rather_than_wrapping_them(self):
        """Wrapping cost three rows; a narrow window now scrolls one row, and
        starts with the current tab in view."""
        self.page.set_viewport_size({"width": 900, "height": 800})
        self.open("chat-settings", self.app.pk)
        found = self.page.evaluate(
            """() => {
              const tabs = document.querySelector('.settings-tabs');
              const links = [...tabs.querySelectorAll('a')];
              const top = (a) => Math.round(a.getBoundingClientRect().top);
              const current = tabs.querySelector('[aria-current]').getBoundingClientRect();
              const strip = tabs.getBoundingClientRect();
              return {
                tops: new Set(links.map(top)).size,
                scrolls: tabs.scrollWidth > tabs.clientWidth,
                visible: current.left >= strip.left && current.right <= strip.right,
              };
            }"""
        )
        self.assertEqual(found["tops"], 1, "the tabs wrapped")
        self.assertTrue(found["scrolls"], "900px was expected to be too narrow for one row")
        self.assertTrue(found["visible"], "the current tab was scrolled out of view")
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
        # Folded behind its summary line; a reader opens it to choose.
        self.page.locator(".answer-settings > summary").click()
        self.page.select_option("#chat-mode", "graph")
        self.page.select_option("#graph-version", "1")
        self.mark_page()
        self.ask("Which deployment serves traffic?")
        answer = self.page.locator(".chat-assistant").last
        answer.get_by_text("carepath-api-green serves traffic.").wait_for()
        # The finished message, rendered by the server once the worker has
        # written it - not just the streamed text, which arrives first.
        self.page.locator('.chat-assistant[data-status="complete"]').last.wait_for()
        self.assertSamePage()
        conversation = ChatConversation.objects.get()
        self.assertEqual(conversation.graph_version, 1)
        self.assertEqual(self.asked, [{"version": 1}])
        self.assertEqual(ChatMessage.objects.get(role="assistant").status, "complete")
        self.assertNoScriptErrors()

    def test_a_lost_response_is_not_asked_twice_and_still_answered(self):
        """The first request reaches the server, which saves it, but the browser
        never hears back. The fallback post carries the same key, and the server
        shows the saved conversation rather than asking - and paying - again.

        Not asking twice is half of it: the saved answer was a placeholder nobody
        was streaming, and it once stayed empty for good. It must resume."""
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

        chat_url = f"{self.live_server_url}{reverse('chat', args=[self.app.pk])}"
        # The chat page only - not the answer's stream or fragment beneath it,
        # which interception would buffer rather than stream.
        self.page.route(
            lambda url: url.split("?")[0] == chat_url,
            lose_the_response,
        )
        self.open("chat", self.app.pk, query="?new=1")
        self.ask("Which deployment serves traffic?")
        self.page.wait_for_url("**conversation=**")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(ChatMessage.objects.filter(role="user").count(), 1)
        conversation = ChatConversation.objects.get()
        self.assertIn(str(conversation.pk), self.page.url)
        self.assertEqual(ChatMessage.objects.filter(role="user").first().conversation, conversation)
        # And the answer arrives: the stranded placeholder resumes on this page.
        self.page.locator(".chat-assistant").last.get_by_text(
            "carepath-api-green serves traffic."
        ).wait_for()
        # The finished message, rendered by the server once the worker has
        # written it - not just the streamed text, which arrives first.
        self.page.locator('.chat-assistant[data-status="complete"]').last.wait_for()
        answer = ChatMessage.objects.get(role="assistant")
        self.assertEqual(answer.status, "complete")
        self.assertEqual(answer.body, "carepath-api-green serves traffic.")
        self.assertEqual(ChatMessage.objects.filter(role="user").count(), 1)
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
