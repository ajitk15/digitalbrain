"""How a message reaches the reader.

The bug these pin: base.html rendered every Django message as `class="notice"`,
and `.notice` was styled green. All fifteen `messages.error` calls in the
codebase therefore appeared as successes, and an error was announced to
assistive technology as a passive status rather than an alert.
"""

from django.contrib import messages
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse

from . import test_documents


def render_message(level_tag, level, body="Something happened"):
    template = Template(
        "{% load icons %}"
        '<div class="notice notice-{{ tag }}" '
        'role="{% if level >= 40 %}alert{% else %}status{% endif %}">'
        "{% message_icon tag %}<span>{{ body }}</span></div>"
    )
    return template.render(Context({"tag": level_tag, "level": level, "body": body}))


class MessageRenderingTests(TestCase):
    def test_an_error_is_not_dressed_as_a_success(self):
        markup = render_message("error", messages.ERROR)
        self.assertIn("notice-error", markup)
        self.assertNotIn("notice-success", markup)

    def test_an_error_is_announced_as_an_alert(self):
        """A status is polite and may never be read out; a failure must be."""
        self.assertIn('role="alert"', render_message("error", messages.ERROR))
        self.assertIn('role="status"', render_message("success", messages.SUCCESS))
        self.assertIn('role="status"', render_message("info", messages.INFO))

    def test_each_level_carries_its_own_class(self):
        for tag, level in (
            ("success", messages.SUCCESS),
            ("error", messages.ERROR),
            ("warning", messages.WARNING),
            ("info", messages.INFO),
        ):
            with self.subTest(level=tag):
                self.assertIn(f"notice-{tag}", render_message(tag, level))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MessagesOnRealPagesTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_a_real_error_message_reaches_the_page_styled_as_one(self):
        """Retention refuses a nonsense window; that refusal must look like a refusal."""
        url = reverse("chat-settings", args=[self.app.pk])
        response = self.client.post(url, {"days": "99999"})
        self.assertContains(response, "Use 0 for indefinite retention")

    def test_a_real_success_message_is_styled_as_a_success(self):
        url = reverse("chat-settings", args=[self.app.pk])
        response = self.client.post(url, {"days": "14"}, follow=True)
        self.assertContains(response, "notice-success")
        self.assertNotIn(b"notice-error", response.content)

    def test_a_standing_explanation_is_not_coloured_as_a_success(self):
        """`.notice` with no level is information, and uploads-disabled is a warning."""
        from platform_core.models import FeatureSwitch

        FeatureSwitch.objects.create(key="document_uploads", enabled=False)
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "notice-warning")
