"""GitHub repository indexing without a checkout or code execution."""

import base64
import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from urllib.parse import quote

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .code_graph_analysis import (
    ANALYZER_VERSION,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    facts,
    included,
    relationships,
)
from .fetching import FetchError, fetch
from .link_sources import github_token
from .models import CodeFile, CodeRelationship, CodeRepository, CodeSnapshot
from .services import audit, feature_enabled
from .workbench import access

API = "https://api.github.com"
logger = logging.getLogger(__name__)


def headers(token):
    result = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def api_json(path, token):
    try:
        body, _, _, _ = fetch(f"{API}{path}", headers=headers(token))
        result = json.loads(body)
    except FetchError as failure:
        message = str(failure)
        if "HTTP 403" in message or "HTTP 429" in message:
            # The unauthenticated limit is 60 requests an hour and one index
            # spends one per file, so this is the first wall most people hit.
            # "HTTP 403" on its own sends them looking for a permission problem.
            raise ValidationError(
                "GitHub refused the request, which is usually its rate limit. "
                "Mount this application's GitHub credential to raise the limit, "
                "or retry later."
            ) from None
        raise ValidationError(f"GitHub: {message}") from None
    except ValueError:
        raise ValidationError("GitHub returned an unexpected response.") from None
    return result


NAME = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")


def valid_name(value):
    """owner/name as two path-safe segments, or a refusal.

    Every row that can reach index_repository goes through this, because the
    repository name is interpolated straight into a GitHub API path. Refresh
    re-queues an existing row, so a row that was never checked on the way in
    would be indexed without ever being checked at all.
    """
    parts = value.split("/")
    if (
        len(parts) != 2
        or not all(parts)
        or any(part in {".", ".."} for part in parts)
        or not NAME.fullmatch(value)
    ):
        raise ValidationError("Enter a GitHub repository as owner/name.")
    return parts


def register(user, app_id, repository, ref=""):
    app, grant = access(user, app_id, "code_graph", write=True)
    if grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    value = repository.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    parts = valid_name(value)
    normalized = f"{parts[0].lower()}/{parts[1].lower()}"
    repo, created = CodeRepository.objects.get_or_create(
        application=app,
        provider="github",
        external_id=normalized,
        defaults={
            "added_by": user,
            "name": value,
            "source_url": f"https://github.com/{value}",
            "default_ref": ref[:200],
            "job_id": uuid.uuid4(),
        },
    )
    if not created:
        repo.default_ref = (ref or repo.default_ref)[:200]
        repo.status = "queued"
        repo.error = ""
        repo.job_id = uuid.uuid4()
        repo.save(update_fields=["default_ref", "status", "error", "job_id", "updated_at"])
    audit(
        user,
        "code_repository.queued",
        repo.pk,
        app.product.portfolio.organization,
        {"repository": value},
    )
    return repo


def showcase_repository(user, app, repository, ref="main"):
    """Make a GitHub source/issue repository visible without silently indexing it.

    Returns None for a name this cannot index. Showcasing is a convenience on
    the side of saving a connector or importing a document, so a name it cannot
    use is a row it does not create - never a failure handed back to someone who
    was doing something else. The connector form accepts an underscore in the
    owner, which GitHub itself does not.
    """
    value = repository.strip().removesuffix(".git")
    try:
        valid_name(value)
    except ValidationError:
        return None
    return CodeRepository.objects.get_or_create(
        application=app,
        provider="github",
        external_id=value.lower(),
        defaults={
            "added_by": user,
            "name": value,
            "source_url": f"https://github.com/{value}",
            "default_ref": (ref or "main")[:200],
            "status": "documentation",
        },
    )[0]


def _decode_blob(data):
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(data.get("content") or "", validate=True)
        if len(raw) > MAX_FILE_BYTES:
            return None
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def index_repository(repository):
    job_id = repository.job_id
    claimed = CodeRepository.objects.filter(
        pk=repository.pk, status="queued", job_id=job_id
    ).update(
        status="indexing", error="", updated_at=timezone.now()
    )
    if not claimed:
        return False
    repository.refresh_from_db()
    try:
        app, grant = access(
            repository.added_by, repository.application_id, "code_graph", write=True
        )
        if grant.role not in {"owner", "contributor"} or not feature_enabled("code_graph", app):
            raise PermissionDenied
        valid_name(repository.name)
        token = github_token(app)
        meta = api_json(f"/repos/{repository.name}", token)
        default_ref = repository.default_ref or meta.get("default_branch") or "main"
        commit = api_json(
            f"/repos/{repository.name}/commits/{quote(default_ref, safe='')}", token
        ).get("sha")
        if not isinstance(commit, str) or not commit:
            raise ValidationError("GitHub did not report a commit for that branch or tag.")
        tree = api_json(f"/repos/{repository.name}/git/trees/{commit}?recursive=1", token)
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list):
            raise ValidationError("GitHub did not return a repository file tree.")
        candidates_by_path = {
            str(item.get("path")): item
            for item in entries
            if isinstance(item, dict)
            and item.get("type") == "blob"
            and included(str(item.get("path") or ""), int(item.get("size") or 0))
        }
        candidates = [candidates_by_path[path] for path in sorted(candidates_by_path)]
        warnings = []
        complete = not bool(tree.get("truncated"))
        if not complete:
            warnings.append(
                "GitHub truncated the repository tree; this snapshot has partial coverage."
            )
        if len(candidates) > MAX_FILES:
            candidates = candidates[:MAX_FILES]
            complete = False
            warnings.append(f"Only the first {MAX_FILES} supported files were indexed.")
        total = 0
        analyzed = []
        for item in candidates:
            size = int(item.get("size") or 0)
            if total + size > MAX_TOTAL_BYTES:
                complete = False
                warnings.append(
                    "The source-size limit was reached; remaining files were not indexed."
                )
                break
            try:
                blob = api_json(f"/repos/{repository.name}/git/blobs/{item['sha']}", token)
            except ValidationError as failure:
                # Whatever was read before the rate limit, the network or a
                # deleted blob is still a true description of this commit.
                # Discarding it means a repository larger than the hourly quota
                # can never produce anything at all, however often it retries.
                if not analyzed:
                    raise
                complete = False
                warnings.append(f"Indexing stopped early: {' '.join(failure.messages)}")
                break
            content = _decode_blob(blob)
            if content is None:
                complete = False
                continue
            total += len(content.encode("utf-8"))
            analyzed.append(facts(item["path"], content))
        manifest = hashlib.sha256(
            json.dumps(
                [(item["path"], item["digest"]) for item in analyzed], separators=(",", ":")
            ).encode()
        ).hexdigest()
        app, current_grant = access(
            repository.added_by, repository.application_id, "code_graph", write=True
        )
        if current_grant.role not in {"owner", "contributor"}:
            raise PermissionDenied
        existing = CodeSnapshot.objects.filter(
            repository=repository, commit_sha=commit, manifest_digest=manifest
        ).first()
        if existing:
            CodeRepository.objects.filter(pk=repository.pk, job_id=job_id).update(
                status="ready" if existing.complete else "partial",
                default_ref=default_ref,
                error="",
            )
            return True
        edges = relationships(analyzed)
        with transaction.atomic():
            locked = CodeRepository.objects.select_for_update().get(pk=repository.pk)
            if locked.job_id != job_id or locked.status != "indexing":
                return False
            number = (
                CodeSnapshot.objects.filter(repository=repository).aggregate(value=Max("number"))[
                    "value"
                ]
                or 0
            ) + 1
            snapshot = CodeSnapshot.objects.create(
                repository=repository,
                number=number,
                ref=default_ref,
                commit_sha=commit,
                manifest_digest=manifest,
                analyzer_version=ANALYZER_VERSION,
                complete=complete,
                warnings=warnings,
            )
            created = {
                item["path"]: CodeFile.objects.create(snapshot=snapshot, **item)
                for item in analyzed
            }
            CodeRelationship.objects.bulk_create(
                [
                    CodeRelationship(
                        snapshot=snapshot,
                        source=created[edge["source"]],
                        target=created[edge["target"]],
                        kind=edge["kind"],
                        confidence=edge["confidence"],
                        detail=edge["detail"],
                        evidence=edge["evidence"],
                    )
                    for edge in edges
                ]
            )
            CodeRepository.objects.filter(pk=repository.pk, job_id=job_id).update(
                status="ready" if complete else "partial", default_ref=default_ref, error=""
            )
            audit(
                repository.added_by,
                "code_repository.indexed",
                repository.pk,
                app.product.portfolio.organization,
                {
                    "snapshot": number,
                    "commit": commit,
                    "files": len(analyzed),
                    "complete": complete,
                },
            )
        return True
    except (ValidationError, PermissionDenied) as failure:
        message = "Access to this application is no longer available."
        if isinstance(failure, ValidationError):
            message = " ".join(failure.messages)
        CodeRepository.objects.filter(
            pk=repository.pk, status="indexing", job_id=job_id
        ).update(
            status="failed", error=message[:500]
        )
        return True
    except Exception:
        CodeRepository.objects.filter(
            pk=repository.pk, status="indexing", job_id=job_id
        ).update(
            status="failed",
            error="Repository indexing stopped unexpectedly. Retry the index.",
        )
        logger.warning(
            "code_repository_index_failed",
            extra={"event": "code_repository_index_failed"},
        )
        return True


def process_next_repository():
    expired = timezone.now() - timedelta(minutes=10)
    CodeRepository.objects.filter(status="indexing", updated_at__lt=expired).update(
        status="queued", job_id=uuid.uuid4(), error="Previous indexing attempt was interrupted."
    )
    repository = CodeRepository.objects.filter(Q(status="queued")).order_by("updated_at").first()
    return index_repository(repository) if repository else False
