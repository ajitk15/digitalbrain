"""Where a document's original bytes are kept: this server's disk, or a bucket.

The converted text is the record (CLAUDE.md, "The converted text is the
record"); the original is what it was converted from, kept for "Convert
again" and for a retried conversion. It was always a file under
`.runtime/documents/<organization>/<application>/<id>.quarantine`, mode 0600 in
a directory mode 0700 - which ties every process that converts to the disk
of the one that received the upload. `[originals] store = "s3"` keeps it in
an S3-compatible bucket instead, under the same key; `"disk"` (the default)
is exactly the old behaviour.

Nothing downstream learns which: the scanner, the extractor subprocess and
the integrity check are handed a local file (`opened`), which for a bucket is
a private temporary copy that is deleted when the block ends - so the offline
extractor still never makes a network call, and a link is still just a
document.

The bucket's keys are the mounted files `originals_access_key_id` and
`originals_secret_access_key`, never configuration and never the
environment. botocore would otherwise read credentials, a profile, a region,
an endpoint override, proxies and a CA bundle from the environment and
`~/.aws`; the client here is built with every one of those stated, so what
the operator configured is all it uses. The endpoint is the operator's, like
the Redis host, so this is not a fetch of a user-supplied URL and does not
go through `fetching`.
"""

import hashlib
import logging
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

#: Seconds to connect and to wait for a reply. An upload is at most 20 MB.
CONNECT_SECONDS = 5
READ_SECONDS = 60
#: Error codes that mean "no such object", which reads as a missing file.
MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
#: Retries are safe here - a PUT of the same bytes to the same key is
#: idempotent - and unlike a model call nothing is billed per attempt.
ATTEMPTS = 3


class StoreUnavailable(OSError):
    """The bucket refused or did not answer: the original is unavailable for now."""


def folder(app):
    """Private, per-application storage for original bytes. Never public static."""
    return (
        Path(settings.BASE_DIR)
        / ".runtime"
        / "documents"
        / str(app.organization_id)
        / str(app.pk)
    )


def name(document_id):
    return f"{document_id}.quarantine"


class Disk:
    """The original as a file on this server, as it always was."""

    kind = "disk"

    def path(self, app, document_id):
        return folder(app) / name(document_id)

    @contextmanager
    def writer(self, app, document_id):
        """A file to write the original into; removed if the block fails."""
        target = self.path(app, document_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                yield destination
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def exists(self, app, document_id):
        return self.path(app, document_id).is_file()

    def delete(self, app, document_id):
        self.path(app, document_id).unlink(missing_ok=True)

    @contextmanager
    def opened(self, app, document_id):
        yield self.path(app, document_id)


class Bucket:
    """The original as an object in an S3-compatible bucket."""

    kind = "s3"

    def __init__(self, config, client=None):
        self.config = config
        self.bucket = config["bucket"]
        self.prefix = config.get("prefix", "").strip("/")
        self._client = client
        self._lock = threading.Lock()

    def key(self, app, document_id):
        parts = [self.prefix] if self.prefix else []
        parts += [str(app.organization_id), str(app.pk), name(document_id)]
        return "/".join(parts)

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                self._client = connect(self.config)
            return self._client

    def call(self, operation, document_id, app, **params):
        """One request for one original; a refusal or silence is an OSError.

        So every caller's existing "the file is unavailable" handling covers a
        bucket that is down, and a missing object is FileNotFoundError, as a
        missing file is. Logged with the operation and the provider's error
        code - never the key's contents beyond ids, never a credential.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(self.client, operation)(
                Bucket=self.bucket, Key=self.key(app, document_id), **params
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in MISSING:
                raise FileNotFoundError(name(document_id)) from None
            failure = code or "refused"
        except BotoCoreError as error:
            failure = type(error).__name__
        logger.warning(
            "originals_unavailable",
            extra={"event": "originals_unavailable", "operation": operation,
                   "failure_kind": failure},
        )
        raise StoreUnavailable(f"The document store did not complete {operation}.")

    def put(self, app, document_id, path):
        options = {}
        if self.config.get("server_side_encryption"):
            options["ServerSideEncryption"] = self.config["server_side_encryption"]
        with open(path, "rb") as body:
            self.call(
                "put_object", document_id, app,
                Body=body, ContentType="application/octet-stream", **options,
            )

    @contextmanager
    def writer(self, app, document_id):
        """A private temporary file, sent to the bucket when the block ends."""
        with private_directory() as scratch:
            path = scratch / name(document_id)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                yield destination
            self.put(app, document_id, path)

    def exists(self, app, document_id):
        try:
            self.call("head_object", document_id, app)
        except FileNotFoundError:
            return False
        return True

    def delete(self, app, document_id):
        # Deleting a key that is not there succeeds, as unlink(missing_ok) does.
        self.call("delete_object", document_id, app)

    @contextmanager
    def opened(self, app, document_id):
        """A private local copy for the scanner and the extractor, removed after.

        A missing object reads as a missing file: the integrity check that
        follows says the stored file is unavailable, as it does on disk.
        """
        with private_directory() as scratch:
            path = scratch / name(document_id)
            try:
                found = self.call("get_object", document_id, app)
            except FileNotFoundError:
                found = None
            if found is not None:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as destination:
                    for chunk in found["Body"].iter_chunks(1024 * 1024):
                        destination.write(chunk)
            yield path


@contextmanager
def private_directory():
    """A temporary directory only this process's user can read, always removed."""
    scratch = Path(tempfile.mkdtemp(prefix="originals-"))
    try:
        os.chmod(scratch, 0o700)
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def credentials():
    from digitalbrain.configuration import read_secret

    directory = settings.SECRET_DIRECTORY
    return (
        read_secret(directory, "originals_access_key_id"),
        read_secret(directory, "originals_secret_access_key"),
    )


def endpoint(config):
    """The configured endpoint, or AWS's own for the region - always stated, so
    `AWS_ENDPOINT_URL_S3` in the environment cannot redirect the bytes."""
    return config.get("endpoint") or f"https://s3.{config['region']}.amazonaws.com"


def isolated_session():
    """A botocore session that reads nothing from the environment or ~/.aws."""
    import botocore.session
    from botocore.configprovider import BOTOCORE_DEFAUT_SESSION_VARIABLES

    variables = {
        variable: (config_name, None, default, converter)
        for variable, (config_name, _, default, converter)
        in BOTOCORE_DEFAUT_SESSION_VARIABLES.items()
    }
    session = botocore.session.Session(session_vars=variables)
    session.set_config_variable("config_file", os.devnull)
    session.set_config_variable("credentials_file", os.devnull)
    return session


def connect(config):
    from botocore.config import Config

    key_id, secret = credentials()
    return isolated_session().create_client(
        "s3",
        region_name=config["region"],
        endpoint_url=endpoint(config),
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        # Stated, so REQUESTS_CA_BUNDLE and AWS_CA_BUNDLE are not consulted.
        verify=config.get("ca_certificate") or True,
        config=Config(
            signature_version="s3v4",
            # An empty mapping, not None: None means "take HTTPS_PROXY".
            proxies={},
            connect_timeout=CONNECT_SECONDS,
            read_timeout=READ_SECONDS,
            retries={"max_attempts": ATTEMPTS, "mode": "standard"},
            # A stand-in such as MinIO is addressed by path; AWS by host.
            s3={"addressing_style": "path" if config.get("endpoint") else "virtual"},
        ),
    )


DISK = Disk()
_buckets = {}
_override = None
_lock = threading.Lock()


def store():
    """The configured store: one Bucket client per configuration per process."""
    if _override is not None:
        return _override
    config = getattr(settings, "ORIGINALS", None) or {}
    if config.get("store") != "s3":
        return DISK
    key = tuple(sorted(config.items()))
    with _lock:
        if key not in _buckets:
            _buckets[key] = Bucket(config)
        return _buckets[key]


def use(replacement):
    """For tests: put a store in place, or None to read the settings again."""
    global _override
    with _lock:
        _override = replacement
        _buckets.clear()


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
