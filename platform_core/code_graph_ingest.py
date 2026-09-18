"""Registering GitHub repositories and indexing them from a local clone.

Acquisition lives in `code_graph_clone`; this module owns registration, the job
lease, and turning one clone into an immutable snapshot. Nothing here fetches a
user-supplied URL - see that module's note on the fetching invariant.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .code_graph_analysis import ANALYZER_VERSION, classify, facts, relationships
from .code_graph_clone import clone_sources
from .link_sources import github_token
from .models import CodeFile, CodeRelationship, CodeRepository, CodeSnapshot
from .services import audit, feature_enabled
from .workbench import access

logger = logging.getLogger(__name__)


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
        # Registering a name that is retired brings it back rather than
        # refusing: the unique constraint still holds that slot, and "you
        # already removed this once" is not a useful answer to someone asking
        # for it again.
        repo.default_ref = (ref or repo.default_ref)[:200]
        repo.status = "queued"
        repo.error = ""
        repo.retired_at = None
        repo.job_id = uuid.uuid4()
        repo.save(
            update_fields=[
                "default_ref",
                "status",
                "error",
                "retired_at",
                "job_id",
                "updated_at",
            ]
        )
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
        commit, sources, warnings, complete = clone_sources(
            repository.name, repository.default_ref, token
        )
        default_ref = repository.default_ref or "HEAD"
        analyzed = [facts(path, content) for path, content in sources]
        if not analyzed:
            warnings.append(
                "No supported source files were indexed. Python, JavaScript, JSX, "
                "TypeScript and TSX are analysed; other languages are not yet."
            )
        # The analyser version is part of what a manifest identifies, not just the
        # bytes it read: the same commit analysed by a better parser is a
        # different set of facts, and (repository, commit, manifest) is unique.
        manifest = hashlib.sha256(
            json.dumps(
                {
                    "analyzer": ANALYZER_VERSION,
                    "files": [(item["path"], item["digest"]) for item in analyzed],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        app, current_grant = access(
            repository.added_by, repository.application_id, "code_graph", write=True
        )
        if current_grant.role not in {"owner", "contributor"}:
            raise PermissionDenied
        # Keyed on the analyser too. The manifest digests file *contents*, so
        # without this a re-index after the analyser improves would hand back a
        # snapshot built by the old one and quietly discard the new facts.
        existing = CodeSnapshot.objects.filter(
            repository=repository,
            commit_sha=commit,
            manifest_digest=manifest,
            analyzer_version=ANALYZER_VERSION,
        ).first()
        if existing:
            # Same rule as a fresh snapshot: nothing indexed is never "ready".
            CodeRepository.objects.filter(pk=repository.pk, job_id=job_id).update(
                status="ready" if existing.complete and analyzed else "partial",
                default_ref=default_ref,
                error="",
            )
            return True
        edges = relationships(analyzed)
        # Over every file in the snapshot, not the subset a page later draws.
        roles, groups, cycle_count = classify([item["path"] for item in analyzed], edges)
        for item in analyzed:
            item["role"] = roles.get(item["path"], "")
            item["cycle_group"] = groups.get(item["path"])
        orphan_count = sum(1 for value in roles.values() if value == "orphan")
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
                cycle_count=cycle_count,
                orphan_count=orphan_count,
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
                status="ready" if complete and analyzed else "partial",
                default_ref=default_ref,
                error="",
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
                    "cycles": cycle_count,
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
