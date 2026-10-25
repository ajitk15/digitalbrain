"""The release check: whether a graph version may become the one that answers.

Publishing decides what chat, Code Factory, ServiceOps and the API trust, and it
used to refuse only a version whose evidence had changed. A version answering
fewer benchmark questions than the published one could be published with one
click and no warning. So a version is now checked the way the scorecard checks a
commit: **against the version that answers now**, by rules, with no model.

Two kinds of check, deliberately:

* **Hard** - the version cannot be trusted at all, and publishing is refused:
  its sources changed since it was built, a relationship quotes text its source
  does not hold, or it read a source that is now restricted. These are integrity
  and safety, never judgement.
* **Soft** - it may be worse than what answers now: fewer benchmark questions
  found, AI that took retrieval away, sources the published version read and
  this one does not, more documents below Ready, a larger share of AI proposals
  refused or outside the ontology. Each compares with the published version,
  never with an ideal, so a version as good as the last one passes. A person may
  publish over a soft failure with a typed reason, stored on the version and in
  the audit event.

"AI added nothing" is said, never failed: on a small library structure alone
often finds as much, and a check that warns on every version trains people to
override it.

The result is stored on the version (`GraphRevision.release`) when it is saved
and whenever it is checked again, for the strip and the Versions table. It is a
report: publishing always checks again.
"""

from dataclasses import asdict, dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

#: Bumped when a check is added or judged differently, so a stored report is
#: never read as the current rules.
RULES_VERSION = 1
#: A reason is a sentence or two, not a document.
REASON_LENGTH = 500
#: A share moving less than this many percentage points is no change.
SAME_PERCENT = 0.5
#: Mean reciprocal rank moving less than this is no change (`graph_trend.SAME`).
SAME_SCORE = 0.005


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    hard: bool
    ok: bool
    detail: str


@dataclass(frozen=True)
class Report:
    number: int
    #: The published version it was compared with, or None when nothing answers yet.
    against: int | None
    checks: tuple

    @property
    def hard_failures(self):
        return [check for check in self.checks if check.hard and not check.ok]

    @property
    def soft_failures(self):
        return [check for check in self.checks if not check.hard and not check.ok]

    @property
    def publishable(self):
        return not self.hard_failures

    @property
    def passed(self):
        return not self.hard_failures and not self.soft_failures

    def stored(self):
        return {
            "rules": RULES_VERSION,
            "checked_at": timezone.now().isoformat(timespec="seconds"),
            "against": self.against,
            "passed": self.passed,
            "publishable": self.publishable,
            "hard": [check.key for check in self.hard_failures],
            "soft": [check.key for check in self.soft_failures],
            "checks": [asdict(check) for check in self.checks],
        }


def stored_state(revision):
    """"passed", "warnings" or "refused" from a version's stored report, or None.

    None when it was never checked, or under other rules: an old report is not
    read as today's verdict.
    """
    report = revision.release or {}
    if report.get("rules") != RULES_VERSION:
        return None
    if not report.get("publishable"):
        return "refused"
    return "passed" if report.get("passed") else "warnings"


def _ids(revision):
    from .graph_store import recorded_sources

    return [source["id"] for source in recorded_sources(revision)]


def _readable(revision, app_id):
    from .graphs import revision_drift

    changed, total = revision_drift(revision, app_id)
    return Check(
        "readable",
        "Every source it read is unchanged",
        True,
        changed == 0,
        f"All {total} sources read as they did when it was built."
        if changed == 0
        else f"{changed} of {total} sources changed or were retired since it was built: "
        "generate a new version.",
    )


def _citations(revision, app_id):
    """Every quoted relationship still verifies: active source, same digest, exact quote."""
    from .graphs import sources_for

    edges = [edge for edge in revision.data.get("edges", []) if edge.get("evidence")]
    ids = {edge.get("knowledge_id") for edge in edges}
    live = {
        str(pk): (digest, content)
        for pk, digest, content in sources_for(app_id)
        .filter(pk__in=[pk for pk in ids if pk])
        .values_list("pk", "digest", "content")
    }
    failing = 0
    for edge in edges:
        found = live.get(edge.get("knowledge_id"))
        if not found or found[0] != edge.get("digest") or edge["evidence"] not in found[1]:
            failing += 1
    return Check(
        "citations",
        "Every quoted relationship verifies",
        True,
        failing == 0,
        f"All {len(edges)} quoted relationships are in their sources, word for word."
        if failing == 0
        else f"{failing} of {len(edges)} quoted relationships no longer verify against "
        "their sources.",
    )


def _restricted(revision, app_id):
    from .models import KnowledgeEntry

    restricted = KnowledgeEntry.objects.filter(
        application_id=app_id, pk__in=_ids(revision), sensitivity="restricted"
    ).count()
    return Check(
        "restricted",
        "No restricted source",
        True,
        restricted == 0,
        "It reads no source restricted from AI."
        if restricted == 0
        else f"It read {restricted} source{'s' if restricted != 1 else ''} since restricted "
        "from AI.",
    )


def _benchmark(app, revision, published):
    from .benchmark import latest_runs

    runs = latest_runs(app, [revision, published])
    mine, theirs = runs.get(revision.pk), runs.get(published.pk)
    if theirs is None:
        return Check(
            "benchmark",
            "Answers the benchmark as well",
            False,
            True,
            "The published version was never scored, so there is nothing to compare."
            if mine is None
            else f"Finds {mine.found} of {mine.questions}; the published version was never "
            "scored.",
        )
    if mine is None:
        return Check(
            "benchmark",
            "Answers the benchmark as well",
            False,
            False,
            f"Not scored yet; the published version finds {theirs.found} of "
            f"{theirs.questions}. Score it on Quality first.",
        )
    worse = mine.found < theirs.found or (
        mine.reciprocal_rank < theirs.reciprocal_rank - SAME_SCORE
    )
    return Check(
        "benchmark",
        "Answers the benchmark as well",
        False,
        not worse,
        f"Finds {mine.found} of {mine.questions} (ranking {mine.reciprocal_rank:.3f}); "
        f"the published version finds {theirs.found} of {theirs.questions} "
        f"({theirs.reciprocal_rank:.3f}).",
    )


def _ai_lift(app, revision):
    from .benchmark import latest_runs
    from .graph_trend import has_ai

    run = latest_runs(app, [revision]).get(revision.pk)
    if not has_ai(revision.data) or run is None or not run.structure:
        return Check(
            "ai_lift",
            "AI does not make retrieval worse",
            False,
            True,
            "Nothing to compare: it has no AI relationships, or was not scored with them "
            "and without.",
        )
    base = run.structure
    took = run.found < base.get("found", 0) or (
        run.reciprocal_rank < base.get("reciprocal_rank", 0) - SAME_SCORE
    )
    added = run.found > base.get("found", 0) or (
        run.reciprocal_rank > base.get("reciprocal_rank", 0) + SAME_SCORE
    )
    return Check(
        "ai_lift",
        "AI does not make retrieval worse",
        False,
        not took,
        f"With AI it finds {run.found}; as structure alone {base.get('found', 0)}. "
        + (
            "The AI relationships took answers away."
            if took
            else "The AI relationships added to it."
            if added
            else "The AI relationships added nothing this benchmark measures."
        ),
    )


def _lost(revision, published):
    from .models import KnowledgeEntry

    gone = set(_ids(published)) - set(_ids(revision))
    titles = list(
        KnowledgeEntry.objects.filter(pk__in=list(gone)[:3]).values_list("title", flat=True)
    )
    return Check(
        "sources_lost",
        "Keeps every source the published version read",
        False,
        not gone,
        "Every source the published version read is in this one."
        if not gone
        else f"{len(gone)} source{'s' if len(gone) != 1 else ''} the published version read "
        f"{'are' if len(gone) != 1 else 'is'} not in this one"
        + (f": {', '.join(titles)}" if titles else "")
        + ("…" if len(gone) > len(titles) else "."),
    )


def _below_ready(app_id, ids):
    from .models import KnowledgeEntry
    from .source_quality import for_entries

    documents = KnowledgeEntry.objects.filter(
        application_id=app_id, pk__in=ids, document__isnull=False
    ).only("pk", "quality", "document_type")
    return sum(1 for found in for_entries(documents).values() if found["verdict"] != "Ready")


def _quality(app_id, revision, published):
    mine = _below_ready(app_id, _ids(revision))
    theirs = _below_ready(app_id, _ids(published))
    return Check(
        "documents_below_ready",
        "No more documents below Ready",
        False,
        mine <= theirs,
        f"{mine} of its documents are below Ready on the source check; the published "
        f"version's sources have {theirs}.",
    )


def _shares(quality):
    kept = quality.get("semantic_relationships") or 0
    refused = quality.get("rejected_relationships") or 0
    outside = quality.get("outside_ontology") or 0
    proposed = kept + refused
    if not proposed:
        return None
    return 100 * refused / proposed, 100 * outside / proposed


def _ontology(revision, published):
    mine, theirs = _shares(revision.quality or {}), _shares(published.quality or {})
    if mine is None or theirs is None:
        return Check(
            "ontology",
            "No larger share of AI proposals refused",
            False,
            True,
            "Nothing to compare: one of the two has no AI proposals.",
        )
    worse = mine[0] > theirs[0] + SAME_PERCENT or mine[1] > theirs[1] + SAME_PERCENT
    return Check(
        "ontology",
        "No larger share of AI proposals refused",
        False,
        not worse,
        f"{mine[0]:.0f}% of its AI proposals were refused and {mine[1]:.0f}% fell outside the "
        f"ontology; the published version's were {theirs[0]:.0f}% and {theirs[1]:.0f}%.",
    )


def check(app, revision, published=None, store=True):
    """The release check for one version against what answers now.

    `published` defaults to the newest published version; a version is never
    compared with itself, and with nothing published only the hard checks run.
    Stores the result on the version unless `store` is False.
    """
    from .graphs import published_revision
    from .models import GraphRevision

    if published is None:
        published = published_revision(app.pk)
    if published is not None and published.pk == revision.pk:
        published = None
    checks = [
        _readable(revision, app.pk),
        _citations(revision, app.pk),
        _restricted(revision, app.pk),
    ]
    if published is not None:
        checks += [
            _benchmark(app, revision, published),
            _ai_lift(app, revision),
            _lost(revision, published),
            _quality(app.pk, revision, published),
            _ontology(revision, published),
        ]
    report = Report(revision.number, published.number if published else None, tuple(checks))
    if store:
        stored = report.stored()
        GraphRevision.objects.filter(pk=revision.pk).update(release=stored)
        revision.release = stored
    return report


def check_after_save(revision_pk):
    """The check for a freshly saved version, after its benchmark. Never fails the save."""
    import logging

    from .models import GraphRevision

    revision = GraphRevision.objects.filter(pk=revision_pk).select_related("application").first()
    if revision is None:
        return
    try:
        check(revision.application, revision)
    except Exception:
        logging.getLogger(__name__).exception(
            "release_check_failed", extra={"event": "release_check_failed"}
        )


def clean_reason(reason):
    reason = " ".join((reason or "").split())
    if len(reason) > REASON_LENGTH:
        raise ValidationError(f"Keep the reason under {REASON_LENGTH} characters.")
    return reason


def publish(user, app, number, reason=""):
    """Check one version and, if it may, make it the one that answers.

    Refused outright on a hard failure. On a soft failure, refused unless a
    reason is given, which is stored on the version. Returns (revision, report).
    """
    from django.db import transaction

    from .models import GraphRevision

    reason = clean_reason(reason)
    revision = GraphRevision.objects.filter(application=app, number=number).first()
    if revision is None:
        raise ValidationError("That graph version does not exist.")
    report = check(app, revision)
    if report.hard_failures:
        raise ValidationError(
            "This version cannot be published: "
            + " ".join(failure.detail for failure in report.hard_failures)
        )
    if report.soft_failures and not reason:
        raise ValidationError(
            "This version did not pass "
            + ", ".join(failure.label.lower() for failure in report.soft_failures)
            + ". Say why it should answer anyway to publish it."
        )
    with transaction.atomic():
        revision.published_at = timezone.now()
        revision.published_by = user
        revision.publish_reason = reason if report.soft_failures else ""
        revision.save(update_fields=["published_at", "published_by", "publish_reason"])
    return revision, report


def published(request, app, revision, report):
    """Audit a publish and say so: one place, for the check's page and the old form."""
    from .services import audit

    details = {"version": revision.number, "against": report.against}
    if report.soft_failures:
        details["warnings"] = [failure.key for failure in report.soft_failures]
        details["reason"] = revision.publish_reason
    audit(
        request.user,
        "graph.published",
        app.pk,
        app.product.portfolio.organization,
        details=details,
    )
    messages.success(
        request,
        f"Version {revision.number} published. Chat and Code Factory now answer from it."
        + (" Your reason is recorded on the version." if report.soft_failures else ""),
    )


@login_required
@require_http_methods(["GET", "POST"])
def release_view(request, pk, number):
    """A version's release check, and the one way the Versions table publishes.

    A page that renders and submits on its own, so the table opens it as a
    popup (`data-modal`) and it still works with JavaScript off. Anyone who can
    read Knowledge sees the check; publishing needs write access, as it always
    did.
    """
    from .models import GraphRevision
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge")
    revision = get_object_or_404(GraphRevision, application=app, number=number)
    error, reason = "", ""
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        reason = request.POST.get("reason", "")
        try:
            revision, report = publish(request.user, app, number, reason)
        except ValidationError as refused:
            error = " ".join(refused.messages)
        else:
            published(request, app, revision, report)
            return redirect(f"{reverse('graph', args=[pk])}?tab=versions")
    report = check(app, revision)
    return render(
        request,
        "graph_release.html",
        {
            "application": app,
            "grant": grant,
            "revision": revision,
            "report": report,
            "error": error,
            "reason": reason[:REASON_LENGTH],
            "can_publish": grant.role in {"owner", "contributor"} and not revision.published_at,
            "reason_length": REASON_LENGTH,
        },
    )
