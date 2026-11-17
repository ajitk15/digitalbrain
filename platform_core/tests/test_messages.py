"""What the screens call things, held to what is true for the person reading them."""

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.models import ApplicationFeature, OrganizationMember
from platform_core.source_quality import readable_check
from platform_core.templatetags.chat_text import inline

from . import test_documents

SETTINGS = {
    "DOCUMENT_AUTO_CONVERT": False,
    "STORAGES": {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    },
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
}


@override_settings(**SETTINGS)
class FeatureOffTests(TestCase):
    """A switched-off feature is said as one, not as a lack of access."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        ApplicationFeature.objects.create(application=self.app, key="service_ops", enabled=False)
        self.url = reverse("serviceops", args=[self.app.pk])

    def test_the_owner_is_told_it_is_off_and_where_to_switch_it_on(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "ServiceOps incident triage is switched off", status_code=403)
        self.assertContains(
            response, reverse("application-features", args=[self.app.pk]), status_code=403
        )
        self.assertNotContains(response, "You do not have access to this", status_code=403)

    def test_a_viewer_is_told_an_owner_can_switch_it_on(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(self.url)
        self.assertContains(response, "is switched off", status_code=403)
        self.assertContains(response, "An owner of the application can", status_code=403)
        self.assertNotContains(
            response, reverse("application-features", args=[self.app.pk]), status_code=403
        )

    def test_someone_with_no_grant_still_learns_nothing(self):
        outsider = type(self.owner).objects.create_user("outsider")
        self.client.force_login(outsider, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 404)


@override_settings(**SETTINGS)
class SidebarRoleTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_an_organization_administrator_is_called_one(self):
        # The fixture's owner administers its organization.
        self.assertContains(self.client.get(reverse("dashboard")), "Organization administrator")

    def test_a_member_is_a_workspace_member(self):
        OrganizationMember.objects.filter(user=self.owner).update(is_admin=False)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Workspace member")
        self.assertNotContains(response, "Organization administrator")


class ChatEmphasisTests(SimpleTestCase):
    def test_user_story_emphasis_renders_rather_than_showing_asterisks(self):
        self.assertEqual(
            inline("*As* Joseph *I want* a score"),
            "<em>As</em> Joseph <em>I want</em> a score",
        )

    def test_arithmetic_code_and_markup_are_left_alone(self):
        self.assertEqual(inline("2 * 3 * 4"), "2 * 3 * 4")
        self.assertEqual(inline("`a*b*c`"), "<code>a*b*c</code>")
        self.assertEqual(inline("<i>*x*</i>"), "&lt;i&gt;<em>x</em>&lt;/i&gt;")


class ShortTextAdviceTests(SimpleTestCase):
    def test_a_short_note_is_not_told_to_upload_a_word_original(self):
        check = readable_check("# Escalation\n\nPage the duty manager.", {})
        self.assertNotIn("Word", check.improve)
        self.assertIn("Add the rest of the document", check.improve)

    def test_a_scan_still_gets_the_scan_advice(self):
        check = readable_check("x", {"pages": 3, "pages_without_text": [1, 2, 3]})
        self.assertIn("Upload the original Word", check.improve)
