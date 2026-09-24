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

from .code_graph_analysis import (
    ANALYZER_VERSION,
    classify,
    describe_languages,
    facts,
    relationships,
)
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
        setup, languages = {}, []
        commit, branch, sources, warnings, complete = clone_sources(
            repository.name, repository.default_ref, token, setup=setup, languages=languages
        )
        # What was asked for, else what the clone landed on, else the
        # convention. "HEAD" used to be the fallback here and it was wrong in
        # the one place it mattered: it reads fine on a snapshot line and is not
        # a branch GitHub can be asked about, so every drift check failed
        # silently and the repository read as "never checked" forever.
        default_ref = repository.default_ref or branch or "main"
        analyzed = [facts(path, content) for path, content in sources]
        if not analyzed:
            found = (
                f" This repository is written in {describe_languages(languages)}."
                if languages
                else ""
            )
            warnings.append(
                f"No supported source files were indexed.{found} Python, JavaScript, "
                "JSX, TypeScript and TSX are analysed; other languages are not yet."
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
            # Also when the detector has learned a language since (the same
            # commit, so the fresh reading is simply the more complete one).
            if setup and set(setup) - set(existing.test_setup or {}):
                CodeSnapshot.objects.filter(pk=existing.pk).update(test_setup=setup)
            # Snapshots taken before the census was recorded learn it here,
            # from the same commit, rather than waiting for the next push.
            if languages and not existing.languages:
                CodeSnapshot.objects.filter(pk=existing.pk).update(languages=languages)
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
                test_setup=setup,
                languages=languages,
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


#: How often a repository's default branch is asked about. A branch moves when
#: somebody pushes, not on a schedule, so this only has to be often enough that
#: "out of sync" appears before anybody wonders.
HEAD_INTERVAL = timedelta(minutes=30)


def refresh_head(repository):
    """Record where the default branch points, without acting on it.

    Detection only, which is the rule knowledge sources already follow: this
    platform notices that an origin has moved and says so. Bringing the change
    in is somebody pressing Refresh index, because re-indexing costs a clone and
    supersedes the snapshot every past run reasoned about.
    """
    from django.utils import timezone

    from .github_write import branch_head
    from .link_sources import github_token

    branch = repository.default_ref or "main"
    token = github_token(repository.application)
    fields = {"head_checked_at": timezone.now()}
    try:
        fields["head_sha"] = branch_head(repository.name, branch, token)
    except ValidationError:
        # A branch that cannot be read is not drift. Leaving head_sha alone
        # keeps the last thing known true rather than claiming the snapshot is
        # current, and the timestamp still moves so this is not retried in a
        # loop.
        pass
    CodeRepository.objects.filter(pk=repository.pk).update(**fields)
    return True


def process_next_head():
    """One repository whose branch is worth asking about again."""
    from django.db.models import Q
    from django.utils import timezone

    due = timezone.now() - HEAD_INTERVAL
    repository = (
        CodeRepository.objects.filter(
            status__in=["ready", "partial"], retired_at__isnull=True
        )
        .filter(Q(head_checked_at__isnull=True) | Q(head_checked_at__lt=due))
        .select_related("application")
        .order_by("head_checked_at")
        .first()
    )
    if repository is None:
        return False
    return refresh_head(repository)


def process_next_repository():
    expired = timezone.now() - timedelta(minutes=10)
    CodeRepository.objects.filter(status="indexing", updated_at__lt=expired).update(
        status="queued", job_id=uuid.uuid4(), error="Previous indexing attempt was interrupted."
    )
    repository = CodeRepository.objects.filter(Q(status="queued")).order_by("updated_at").first()
    return index_repository(repository) if repository else False
