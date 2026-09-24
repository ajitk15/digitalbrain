"""The shared icon registry.

Icons used to be pasted as full inline SVG at each call site - the application
menu alone carried four copies keyed on the label text. They now come from one
registry, so a name that does not exist must fail loudly rather than rendering
an invisible gap nobody notices until a screenshot.
"""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.templatetags.icons import ICONS, MESSAGE_ICONS


def render(source, **context):
    return Template("{% load icons %}" + source).render(Context(context))


class IconTests(SimpleTestCase):
    def test_an_icon_renders_as_decorative_svg(self):
        markup = render('{% icon "graph" %}')
        self.assertIn("<svg", markup)
        self.assertIn('aria-hidden="true"', markup)
        # It sits beside its own label, so it must not also be a focus stop.
        self.assertNotIn("<title", markup)

    def test_an_unknown_icon_fails_loudly(self):
        with self.assertRaises(TemplateSyntaxError) as raised:
            render('{% icon "no-such-icon" %}')
        self.assertIn("no-such-icon", str(raised.exception))

    def test_the_size_is_applied_to_both_dimensions(self):
        markup = render('{% icon "chat" 13 %}')
        self.assertIn('width="13"', markup)
        self.assertIn('height="13"', markup)

    def test_path_data_is_not_escaped_into_visible_text(self):
        self.assertIn("<path", render('{% icon "chat" %}'))

    def test_every_message_level_maps_to_a_registered_icon(self):
        for name in MESSAGE_ICONS.values():
            self.assertIn(name, ICONS)

    def test_an_error_message_gets_the_error_icon_not_the_success_one(self):
        """Every messages.error used to render in the success style."""
        self.assertEqual(render("{% message_icon 'error' %}"), render('{% icon "error" %}'))
        self.assertNotEqual(render("{% message_icon 'error' %}"), render('{% icon "success" %}'))

    def test_an_unknown_level_falls_back_to_information(self):
        self.assertEqual(render("{% message_icon 'weird' %}"), render('{% icon "info" %}'))
        self.assertEqual(render("{% message_icon '' %}"), render('{% icon "info" %}'))


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class FaviconTests(TestCase):
    """The tab icon: named by every page, and answered at the bare path."""

    def test_the_sign_in_page_names_the_icon(self):
        body = self.client.get(reverse("login")).content.decode()
        self.assertIn('rel="icon"', body)
        self.assertIn("brand/favicon.ico", body)
        self.assertIn('rel="apple-touch-icon"', body)

    def test_the_bare_path_is_sent_to_the_icon(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("brand/favicon.ico"))

    def test_the_icon_files_exist(self):
        from pathlib import Path

        from django.conf import settings

        brand = Path(settings.BASE_DIR) / "static" / "brand"
        for name in ("favicon.ico", "favicon-32.png", "apple-touch-icon.png"):
            self.assertTrue((brand / name).is_file(), name)

