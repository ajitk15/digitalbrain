import base64
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.fetching import FetchError, normalise
from platform_core.link_sources import submit
from platform_core.models import Document
from platform_core.sharepoint import (
    _token_cache,
    download_url,
    is_sharepoint,
    plan,
    share_id,
)

from . import test_documents

TENANT = {"SHAREPOINT_TENANT": "contoso.onmicrosoft.com", "SHAREPOINT_CLIENT_ID": "abc-123"}


class RecognitionTests(SimpleTestCase):
    def test_every_sharepoint_and_onedrive_host_is_recognised(self):
        for url in [
            "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/spec.docx",
            "https://contoso.sharepoint.com/:w:/r/sites/Team/_layouts/15/Doc.aspx?sourcedoc=x",
            "https://contoso-my.sharepoint.com/:b:/g/personal/a_contoso_com/EaBc",
            "https://graph.microsoft.com/v1.0/drives/d/items/i",
        ]:
            with self.subTest(url=url):
                self.assertTrue(is_sharepoint(normalise(url)))

    def test_other_hosts_are_not_sharepoint(self):
        for url in ["https://github.com/a/b", "https://example.com/sharepoint.com/x"]:
            with self.subTest(url=url):
                self.assertFalse(is_sharepoint(normalise(url)))

    def test_a_sharing_url_is_encoded_the_way_graph_expects(self):
        """u! + unpadded base64url. Getting this wrong silently 404s every link."""
        url = "https://contoso.sharepoint.com/:w:/r/sites/T/Doc.aspx?sourcedoc=%7Bab%7D"
        encoded = share_id(url)
        self.assertTrue(encoded.startswith("u!"))
        self.assertNotIn("=", encoded)
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)
        restored = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
        self.assertEqual(base64.urlsafe_b64decode(restored).decode(), url)


@override_settings(**TENANT)
class PlanTests(SimpleTestCase):
    def setUp(self):
        _token_cache.clear()

    def graph(self, *responses):
        return patch("platform_core.sharepoint._graph", side_effect=list(responses))

    def token(self):
        return patch("platform_core.sharepoint.access_token", return_value="tok")

    def file_item(self, name="spec.docx"):
        return {
            "id": "ITEM1",
            "name": name,
            "file": {},
            "parentReference": {"driveId": "DRIVE1"},
        }

    def test_a_file_link_resolves_to_one_durable_graph_reference(self):
        with self.token(), self.graph(self.file_item()):
            files = plan(normalise("https://contoso.sharepoint.com/x"), "secret")
        self.assertEqual(len(files), 1)
        name, url = files[0]
        self.assertEqual(name, "spec.docx")
        # A durable item address, not the short-lived download URL.
        self.assertEqual(url, "https://graph.microsoft.com/v1.0/drives/DRIVE1/items/ITEM1")
        self.assertNotIn("content", url)

    def test_a_folder_link_resolves_to_its_documents(self):
        folder = {"id": "F", "name": "Handbook", "folder": {}, "parentReference": {"driveId": "D"}}
        children = {
            "value": [
                self.file_item("one.docx"),
                self.file_item("two.pdf"),
                {"id": "S", "name": "sub", "folder": {}, "parentReference": {"driveId": "D"}},
                self.file_item("image.png"),
            ]
        }
        with self.token(), self.graph(folder, children):
            files = plan(normalise("https://contoso.sharepoint.com/folder"), "secret")
        self.assertEqual([n for n, _ in files], ["Handbook-one.docx", "Handbook-two.pdf"])

    def test_a_folder_of_unsupported_files_says_so(self):
        folder = {"id": "F", "name": "Pics", "folder": {}, "parentReference": {"driveId": "D"}}
        children = {"value": [self.file_item("a.png"), self.file_item("b.zip")]}
        with self.token(), self.graph(folder, children):
            with self.assertRaises(FetchError):
                plan(normalise("https://contoso.sharepoint.com/folder"), "secret")

    def test_an_unopenable_item_is_reported_clearly(self):
        with self.token(), self.graph({"error": {"code": "accessDenied"}}):
            with self.assertRaises(FetchError) as raised:
                plan(normalise("https://contoso.sharepoint.com/x"), "secret")
        self.assertIn("granted access", str(raised.exception))

    def test_download_reads_a_fresh_address_rather_than_following_a_redirect(self):
        item = {
            "id": "ITEM1",
            "name": "spec.docx",
            "@microsoft.graph.downloadUrl": "https://storage.example/abc",
        }
        with self.token(), self.graph(item):
            url, name = download_url("https://graph.microsoft.com/v1.0/drives/D/items/I", "s")
        self.assertEqual(url, "https://storage.example/abc")
        self.assertEqual(name, "spec.docx")

    def test_a_missing_download_address_is_an_error_not_a_silent_empty_file(self):
        with self.token(), self.graph({"id": "I", "name": "spec.docx"}):
            with self.assertRaises(FetchError):
                download_url("https://graph.microsoft.com/v1.0/drives/D/items/I", "s")


class ConfigurationTests(SimpleTestCase):
    def setUp(self):
        _token_cache.clear()

    @override_settings(SHAREPOINT_TENANT="", SHAREPOINT_CLIENT_ID="")
    def test_an_unconfigured_deployment_says_what_is_missing(self):
        from platform_core.sharepoint import access_token

        with self.assertRaises(FetchError) as raised:
            access_token("secret")
        self.assertIn("sharepoint_tenant", str(raised.exception))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    **TENANT,
)
class SharePointImportTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        # contoso is Microsoft's documentation tenant and does not resolve; the
        # submit-time address check would otherwise fail before reaching the logic
        # under test.
        import socket

        records = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("13.107.136.9", 443))
        ]
        resolver = patch("socket.getaddrinfo", return_value=records)
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_an_application_without_a_mounted_secret_cannot_reach_sharepoint(self):
        """Mounting the secret is the per-application gate, even when the
        deployment is configured."""
        with self.assertRaises(ValidationError) as raised:
            submit(self.owner, self.app.pk, "https://contoso.sharepoint.com/sites/T/a.docx")
        self.assertIn("not enabled for this application", str(raised.exception))
        self.assertFalse(Document.objects.filter(origin="sharepoint").exists())

    def test_a_configured_application_queues_the_documents_a_link_resolves_to(self):
        files = [("Handbook-one.docx", "https://graph.microsoft.com/v1.0/drives/D/items/1")]
        with (
            patch("platform_core.link_sources.mounted", return_value="client-secret"),
            patch("platform_core.link_sources.sharepoint_plan", return_value=files),
        ):
            created = submit(self.owner, self.app.pk, "https://contoso.sharepoint.com/f")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].origin, "sharepoint")
        self.assertEqual(created[0].status, "pending")
