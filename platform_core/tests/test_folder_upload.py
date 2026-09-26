"""Uploading a folder, subfolders included, as one source.

The folder is chosen with the browser's own picker and its files are uploaded;
this server never reads a path somebody typed. Each file's place inside the
folder becomes its name, the way a GitHub folder import names them, and the
whole upload is one source - so demo reset clears it with the link sources.
"""

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import demo_reset
from platform_core.documents import folder_names
from platform_core.knowledge_sources import process_next_source, resync
from platform_core.models import Document, KnowledgeSource

from . import test_documents

FILES = {
    "docs/00-overview/README.md": b"# CarePath",
    "docs/02-design/adr/0002-static-token-directory.md": b"# ADR 2",
    "docs/.git/config": b"[core]",
    "docs/Thumbs.db": b"x",
}


def upload(path, content):
    return SimpleUploadedFile(path.rsplit("/", 1)[-1], content)


class FolderNameTests(SimpleTestCase):
    def names(self, paths, files=None):
        uploads = [upload(path, b"x") for path in (files or paths)]
        top, named = folder_names(uploads, json.dumps(paths) if paths is not None else "")
        return top, [name for _, name in named]

    def test_the_chosen_folder_names_the_source_and_subfolders_stay_in_names(self):
        top, names = self.names(list(FILES))
        self.assertEqual(top, "docs")
        # Hidden folders and OS clutter are not documents.
        self.assertEqual(
            names, ["00-overview/README.md", "02-design/adr/0002-static-token-directory.md"]
        )

    def test_a_path_that_climbs_or_names_another_file_is_only_its_own_name(self):
        _, names = self.names(
            ["docs/../../secrets/key.md", "docs/a/other.md"], files=["docs/key.md", "docs/a/b.md"]
        )
        self.assertEqual(names, ["key.md", "b.md"])

    def test_without_paths_every_file_keeps_its_bare_name(self):
        top, names = self.names(None, files=["docs/a/one.md", "docs/two.md"])
        self.assertEqual((top, names), ("Uploaded folder", ["one.md", "two.md"]))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class FolderUploadTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("source-add", args=[self.app.pk])

    def post(self, files=FILES):
        return self.client.post(
            self.url,
            {
                "action": "folder",
                "folder": [upload(path, content) for path, content in files.items()],
                "paths": json.dumps(list(files)),
            },
        )

    def test_the_page_offers_a_folder_picker(self):
        page = self.client.get(self.url)
        self.assertContains(page, "webkitdirectory")
        self.assertContains(page, 'name="action" value="folder"')

    def test_a_folder_becomes_one_source_with_its_subfolders_in_the_names(self):
        self.assertEqual(self.post().status_code, 302)
        source = KnowledgeSource.objects.get()
        described = (source.provider, source.name, source.status)
        self.assertEqual(described, ("folder", "docs", "uploaded"))
        self.assertEqual(
            sorted(Document.objects.filter(source=source).values_list("name", flat=True)),
            ["00-overview/README.md", "02-design/adr/0002-static-token-directory.md"],
        )
        self.assertTrue(all(d.origin == "folder" for d in Document.objects.all()))

    def test_uploading_it_again_skips_what_is_unchanged_and_replaces_what_changed(self):
        self.post()
        changed = {**FILES, "docs/00-overview/README.md": b"# CarePath, revised"}
        self.post(changed)
        self.assertEqual(KnowledgeSource.objects.count(), 1)
        live = Document.objects.exclude(status="deleted")
        self.assertEqual(live.count(), 2)
        self.assertEqual(Document.objects.filter(status="deleted").count(), 1)

    def test_a_folder_is_never_checked_or_resynchronised(self):
        self.post()
        self.assertFalse(process_next_source())
        source = KnowledgeSource.objects.get()
        with self.assertRaisesMessage(Exception, "Upload the folder again"):
            resync(self.owner, self.app.pk, source.pk)

    def test_the_sources_list_offers_no_origin_to_open_or_resync(self):
        self.post()
        page = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(page, "Folder uploaded by owner")
        self.assertNotContains(page, "folder:docs")

    def test_a_viewer_cannot_upload_a_folder(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(KnowledgeSource.objects.exists())

    @override_settings(ALLOW_DEMO_RESET=True)
    def test_demo_reset_clears_a_folder_upload(self):
        self.post()
        demo_reset.reset(self.admin, self.app.product.portfolio.organization, "Example")
        self.assertFalse(KnowledgeSource.objects.exists())
        self.assertFalse(Document.objects.exists())
