"""Originals in an S3-compatible bucket (`originals.py`, `[originals] store = "s3"`).

What leaves the server - one endpoint, this deployment's key, nothing from
the environment - is checked against botocore itself, with the request caught
before it is sent. The document flows are checked against an in-memory bucket
that behaves as S3 does for the four calls used: upload, convert, convert
again, retry, delete and the keep-original switch read and write the bucket
and leave nothing on this server's disk, and a bucket that does not answer
is never mistaken for an original that is gone.
"""

import hashlib
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain.configuration import load_config
from platform_core import originals
from platform_core.documents import document_path, store_document
from platform_core.models import Document
from platform_core.processing import convert_document

from . import test_documents
from .test_document_originals import SETTINGS

CONFIG = {"store": "s3", "bucket": "brain-originals", "region": "eu-west-2"}


class Body:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, size):
        for start in range(0, len(self.data), size):
            yield self.data[start:start + size]


class FakeS3:
    """The four calls `Bucket` makes, as S3 answers them."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.down = False

    def check(self, operation, bucket, key):
        self.calls.append((operation, bucket, key))
        if self.down:
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    def missing(self, operation, code="404"):
        return ClientError({"Error": {"Code": code, "Message": "Not Found"}}, operation)

    def put_object(self, Bucket, Key, Body, ContentType, **options):
        self.check("put_object", Bucket, Key)
        self.objects[(Bucket, Key)] = (Body.read(), options)

    def head_object(self, Bucket, Key):
        self.check("head_object", Bucket, Key)
        if (Bucket, Key) not in self.objects:
            raise self.missing("HeadObject")
        return {}

    def get_object(self, Bucket, Key):
        self.check("get_object", Bucket, Key)
        if (Bucket, Key) not in self.objects:
            raise self.missing("GetObject", "NoSuchKey")
        return {"Body": Body(self.objects[(Bucket, Key)][0])}

    def delete_object(self, Bucket, Key):
        self.check("delete_object", Bucket, Key)
        self.objects.pop((Bucket, Key), None)


class Raw:
    """An empty raw HTTP body, as botocore reads one."""

    def stream(self, **kwargs):
        yield b""


class SecretFiles:
    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        for name, value in {
            "originals_access_key_id": "AKIDAPPLICATION",
            "originals_secret_access_key": "application-secret",
        }.items():
            path = Path(directory.name) / name
            path.write_text(value, encoding="utf-8")
            os.chmod(path, 0o600)
        self.directory = directory.name


class ClientIsolationTests(SimpleTestCase):
    """The real botocore client, its request caught just before it would be sent."""

    def capture(self, config):
        secrets = SecretFiles(self)
        aws = tempfile.TemporaryDirectory()
        self.addCleanup(aws.cleanup)
        profile = Path(aws.name) / "config"
        profile.write_text(
            "[profile operator]\nendpoint_url = https://profile.attacker.example\n"
            "region = us-east-1\n",
            encoding="utf-8",
        )
        planted = {
            "AWS_ACCESS_KEY_ID": "AKIDOPERATOR",
            "AWS_SECRET_ACCESS_KEY": "operator-secret",
            "AWS_SESSION_TOKEN": "operator-session",
            "AWS_PROFILE": "operator",
            "AWS_CONFIG_FILE": str(profile),
            "AWS_ENDPOINT_URL": "https://env.attacker.example",
            "AWS_ENDPOINT_URL_S3": "https://env-s3.attacker.example",
            "AWS_CA_BUNDLE": str(Path(aws.name) / "planted.pem"),
            "REQUESTS_CA_BUNDLE": str(Path(aws.name) / "planted.pem"),
            "HTTPS_PROXY": "http://proxy.attacker.example:3128",
            "HTTP_PROXY": "http://proxy.attacker.example:3128",
        }
        sent = []

        def before_send(request, **kwargs):
            sent.append(request)
            return AWSResponse(request.url, 200, {}, Raw())

        with (
            patch.dict(os.environ, planted),
            override_settings(SECRET_DIRECTORY=secrets.directory),
        ):
            client = originals.connect(config)
            client.meta.events.register("before-send", before_send)
            client.put_object(Bucket=config["bucket"], Key="o/a/d.quarantine", Body=b"x")
        return client, sent

    def test_the_request_goes_where_the_operator_said_with_the_mounted_key(self):
        endpoint = {**CONFIG, "endpoint": "http://minio:9000"}
        client, sent = self.capture(endpoint)
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.url, "http://minio:9000/brain-originals/o/a/d.quarantine")
        authorization = request.headers["Authorization"].decode()
        self.assertIn("Credential=AKIDAPPLICATION/", authorization)
        self.assertIn("/eu-west-2/s3/", authorization)
        self.assertNotIn("X-Amz-Security-Token", request.headers)
        session = client._endpoint.http_session
        # HTTPS_PROXY is planted; the client was given no proxy to use.
        for url in ("http://minio:9000", "https://s3.eu-west-2.amazonaws.com"):
            self.assertIsNone(session._proxy_config.proxy_url_for(url))
        self.assertIs(session._verify, True)

    def test_without_an_endpoint_it_is_aws_for_the_region_not_the_environments(self):
        _, sent = self.capture(CONFIG)
        self.assertEqual(
            sent[0].url, "https://brain-originals.s3.eu-west-2.amazonaws.com/o/a/d.quarantine"
        )

    def test_a_named_ca_certificate_is_the_one_verified_against(self):
        client, _ = self.capture({**CONFIG, "ca_certificate": "/etc/ssl/bucket-ca.pem"})
        self.assertEqual(client._endpoint.http_session._verify, "/etc/ssl/bucket-ca.pem")

    def test_without_the_mounted_keys_nothing_is_sent(self):
        with tempfile.TemporaryDirectory() as empty, override_settings(SECRET_DIRECTORY=empty):
            with self.assertRaisesMessage(ImproperlyConfigured, "originals_access_key_id"):
                originals.connect(CONFIG)


class BucketTests(SimpleTestCase):
    class App:
        organization_id = "org-1"
        pk = "app-1"

    def bucket(self, **config):
        fake = FakeS3()
        return originals.Bucket({**CONFIG, **config}, client=fake), fake

    def test_keys_are_the_disk_layout_under_an_optional_prefix(self):
        bucket, _ = self.bucket()
        self.assertEqual(bucket.key(self.App, "d"), "org-1/app-1/d.quarantine")
        bucket, _ = self.bucket(prefix="/digital-brain/")
        self.assertEqual(bucket.key(self.App, "d"), "digital-brain/org-1/app-1/d.quarantine")

    def test_a_written_original_is_read_back_from_a_private_copy_that_is_then_removed(self):
        bucket, fake = self.bucket(server_side_encryption="AES256")
        with bucket.writer(self.App, "d") as destination:
            destination.write(b"original bytes")
        data, options = fake.objects[("brain-originals", "org-1/app-1/d.quarantine")]
        self.assertEqual((data, options), (b"original bytes", {"ServerSideEncryption": "AES256"}))
        with bucket.opened(self.App, "d") as path:
            self.assertEqual(path.read_bytes(), b"original bytes")
            if os.name != "nt":
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_a_failed_write_sends_nothing(self):
        bucket, fake = self.bucket()
        with self.assertRaises(RuntimeError):
            with bucket.writer(self.App, "d") as destination:
                destination.write(b"half")
                raise RuntimeError("upload too large")
        self.assertEqual(fake.objects, {})

    def test_a_missing_object_is_a_missing_file_and_a_silent_bucket_is_not(self):
        bucket, fake = self.bucket()
        self.assertFalse(bucket.exists(self.App, "d"))
        with bucket.opened(self.App, "d") as path:
            self.assertFalse(path.exists())
        bucket.delete(self.App, "d")
        fake.down = True
        with self.assertLogs("platform_core.originals", "WARNING") as logged:
            with self.assertRaises(originals.StoreUnavailable):
                bucket.exists(self.App, "d")
        self.assertNotIn("application-secret", "".join(logged.output))
        self.assertTrue(issubclass(originals.StoreUnavailable, OSError))

    def test_the_disk_store_unless_a_bucket_is_configured(self):
        with override_settings(ORIGINALS={}):
            self.assertIs(originals.store(), originals.DISK)
        with override_settings(ORIGINALS=CONFIG):
            first = originals.store()
            self.assertIsInstance(first, originals.Bucket)
            self.assertIs(originals.store(), first, "one client per configuration")


@override_settings(**{**SETTINGS, "DOCUMENT_AUTO_CONVERT": False}, ORIGINALS=CONFIG)
class BucketDocumentTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.s3 = FakeS3()
        originals.use(originals.Bucket(CONFIG, client=self.s3))
        self.addCleanup(originals.use, None)

    def key(self, document):
        return ("brain-originals", f"{self.app.organization_id}/{self.app.pk}/{document.pk}"
                ".quarantine")

    def upload(self, name="policy.md", content=b"# Refunds\n\nRefunds expire after thirty days.\n"):
        return store_document(self.owner, self.app.pk, SimpleUploadedFile(name, content))

    def test_an_upload_goes_to_the_bucket_and_never_to_this_servers_disk(self):
        content = b"# Refunds\n\nRefunds expire after thirty days.\n"
        document = self.upload(content=content)
        self.assertEqual(self.s3.objects[self.key(document)][0], content)
        self.assertEqual(document.sha256, hashlib.sha256(content).hexdigest())
        self.assertFalse(document_path(self.app, document.pk).exists())

    def test_conversion_reads_the_bucket_and_convert_again_is_offered(self):
        document = self.upload()
        entry = convert_document(self.owner, self.app.pk, document.pk)
        self.assertIn("thirty days", entry.content)
        detail = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertTrue(detail.context["can_reconvert"])

    def test_an_original_the_bucket_lost_fails_its_integrity_check(self):
        document = self.upload()
        self.s3.objects.clear()
        with self.assertRaisesMessage(ValidationError, "unavailable or its integrity check"):
            convert_document(self.owner, self.app.pk, document.pk)
        self.assertEqual(Document.objects.get(pk=document.pk).status, "failed")

    def test_a_bucket_that_does_not_answer_is_not_a_lost_original(self):
        document = self.upload()
        self.s3.down = True
        with self.assertLogs("platform_core.originals", "WARNING"):
            with self.assertRaisesMessage(ValidationError, "not answering"):
                convert_document(self.owner, self.app.pk, document.pk)
        failed = Document.objects.get(pk=document.pk)
        self.assertEqual(failed.status, "failed")
        with self.assertLogs("platform_core.originals", "WARNING"):
            response = self.client.post(
                reverse("document-retry", args=[self.app.pk, document.pk]), follow=True
            )
        self.assertContains(response, "not answering")
        self.assertEqual(Document.objects.get(pk=document.pk).status, "failed")
        self.s3.down = False
        self.client.post(reverse("document-retry", args=[self.app.pk, document.pk]))
        self.assertEqual(Document.objects.get(pk=document.pk).status, "queued")

    @override_settings(KEEP_ORIGINAL_DOCUMENTS=False)
    def test_dropping_originals_deletes_the_object_once_the_text_is_stored(self):
        document = self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            convert_document(self.owner, self.app.pk, document.pk)
        self.assertNotIn(self.key(document), self.s3.objects)
        self.assertIsNotNone(Document.objects.get(pk=document.pk).original_removed_at)

    def test_deleting_a_document_deletes_its_object(self):
        document = self.upload()
        self.client.post(reverse("document-delete", args=[self.app.pk, document.pk]))
        self.assertNotIn(self.key(document), self.s3.objects)
        self.assertEqual(Document.objects.get(pk=document.pk).status, "deleted")

    def test_an_upload_the_bucket_refuses_keeps_no_row(self):
        self.s3.down = True
        with self.assertLogs("platform_core.originals", "WARNING"):
            with self.assertRaises(OSError):
                self.upload()
        self.assertFalse(Document.objects.exists())


@override_settings(**{**SETTINGS, "DOCUMENT_AUTO_CONVERT": False})
class MoveOriginalsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_disk_originals_move_into_the_bucket_after_their_digest_is_checked(self):
        kept = store_document(self.owner, self.app.pk, SimpleUploadedFile("a.md", b"# A\n"))
        tampered = store_document(self.owner, self.app.pk, SimpleUploadedFile("b.md", b"# B\n"))
        document_path(self.app, tampered.pk).write_bytes(b"# changed on disk\n")
        s3 = FakeS3()
        originals.use(originals.Bucket(CONFIG, client=s3))
        self.addCleanup(originals.use, None)
        with self.assertRaisesMessage(CommandError, "2 originals are still on disk"):
            call_command("move_originals", "--check")
        out = io.StringIO()
        with self.assertRaisesMessage(CommandError, "1 originals did not match"):
            call_command("move_originals", stdout=out)
        self.assertIn("moved 1, left 1", out.getvalue())
        self.assertFalse(document_path(self.app, kept.pk).exists())
        self.assertTrue(document_path(self.app, tampered.pk).exists())
        self.assertEqual(
            s3.objects[("brain-originals", originals.store().key(self.app, kept.pk))][0],
            b"# A\n",
        )

    def test_there_is_nothing_to_move_to_without_a_bucket(self):
        with self.assertRaisesMessage(CommandError, 'store = "s3"'):
            call_command("move_originals")


class OriginalsSettingTests(SimpleTestCase):
    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body, mode="development"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'mode = "{mode}"\n' + self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    TABLE = '[originals]\nstore = "s3"\nbucket = "brain-originals"\nregion = "eu-west-2"\n'

    def test_disk_by_default_and_a_bucket_when_named(self):
        self.assertEqual(self.load("").get("originals", {}), {})
        loaded = self.load(self.TABLE + 'endpoint = "http://minio:9000"\n')
        self.assertEqual(loaded["originals"]["endpoint"], "http://minio:9000")

    def test_credentials_never_live_in_the_configuration(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "cannot contain credentials"):
            self.load(self.TABLE + 'secret_access_key = "x"\n')

    def test_production_sends_originals_over_https_only(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "https in production"):
            self.load(self.TABLE + 'endpoint = "http://minio:9000"\n', mode="production")
        for endpoint in ("https://user:pw@s3.example", "https://s3.example/path", "minio:9000"):
            with self.assertRaises(ImproperlyConfigured):
                self.load(self.TABLE + f'endpoint = "{endpoint}"\n')

    def test_the_rest_is_checked_too(self):
        for line in (
            'bucket = "Not_A_Bucket"\n',
            'store = "nfs"\n',
            'prefix = "../escape"\n',
            'server_side_encryption = "rot13"\n',
            'ca_certificate = "relative.pem"\n',
        ):
            body = self.TABLE.replace('bucket = "brain-originals"\n', "") if "bucket" in line \
                else self.TABLE
            body = body.replace('store = "s3"\n', "") if "store" in line else body
            with self.assertRaises(ImproperlyConfigured, msg=line):
                self.load(body + line)
        with self.assertRaisesMessage(ImproperlyConfigured, 'need store = "s3"'):
            self.load('[originals]\nbucket = "brain-originals"\n')
