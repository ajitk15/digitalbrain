"""What using the knowledge teaches, offered back to a person (Knowledge → Learned).

Using an application's knowledge produces records the platform already keeps:
a Code Factory change delivered after an approved plan, an incident cause a
person confirmed from triage's own citations, a question nothing answered, an
answer someone marked wrong. None of it reached Sources, so the knowledge never
learned from its own use. This turns each into a `LearningCandidate`:

* **Rules write it, never a model.** A candidate's text is put together from
  stored fields. What a model wrote - a plan item's title, triage's statement of
  the cause - is quoted as what the plan or triage said, never stated as fact.
* **A person accepts it.** An owner or contributor, audited. A change record or
  an incident learning becomes a source through `workbench.add_knowledge` -
  masking, the personal-data scan and restriction apply as to any other - and
  then goes through Build, Check and Publish like everything else. A question
  becomes a benchmark question only once someone names the source it should
  cite, which makes the release check harder to pass as the application is
  used. A rejected candidate is remembered, so it is not offered again.
* **It is derived knowledge.** Learned sources are recorded under
  `LEARNED_PREFIX`, which `code_knowledge.references` excludes beside
  `DERIVED_PREFIX`: a change record can tell chat "CARE-401 was changed in these
  files", but it can never be the evidence that code meets a requirement - the
  platform would be citing its own output as proof of itself.
* **A withdrawn signal withdraws its candidate.** A retracted verdict retires the
  incident learning made from it; a conversation removed by its owner or by chat
  retention takes its question out of the inbox, so the inbox never keeps what
  retention removed. A question that holds personal data is never copied here.

A rejected Code Factory plan item is deliberately not a signal: a rejection is a
judgement, not a fact, and it stays on its run.

Questions people asked are shown, without who asked, to the application's owners
and contributors - the people who decide what it knows. That is the one thing
here that reaches anyone new, and the Learned page and the report form say so.
"""

import logging
import time
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .code_knowledge import LEARNED_PREFIX
from .services import audit

logger = logging.getLogger(__name__)

#: How far back a signal is read. Older work was learned from already, or not.
WINDOW_DAYS = 90
#: Signals read per kind per collection, newest first.
BATCH = 50
#: How often the maintenance lane collects for every application.
COLLECT_SECONDS = 600
#: What a check may conclude and still count as passing.
PASSING = {"success", "neutral", "skipped"}
#: The inbox shows this many decided candidates under the open ones.
DECIDED_SHOWN = 30

_last_collect = None


def source_for(candidate):
    """Where an accepted candidate is recorded: one address per signal."""
    signal = candidate.run_id or candidate.verdict_id
    return f"{LEARNED_PREFIX}{candidate.kind}/{signal}"


def person(user):
    return (user.get_full_name() or user.get_username()) if user else "someone"


def change_text(run):
    """(title, body) for a delivered change, from the run's stored fields."""
    plan = run.plan
    ticket = run.ticket_external_id
    title = (
        f"Change record: {ticket} {run.ticket_title}".strip()
        if ticket
        else f"Change record: Code Factory run {run.number}"
    )
    lines = [f"# {title}", "", "Type: Change record"]
    if ticket:
        lines.append(f"Ticket: {ticket}")
    if run.ticket_title:
        lines.append(f"Ticket title: {run.ticket_title}")
    lines.append(f"Code Factory run: {run.number}")
    if run.pull_request_url:
        lines.append(f"Pull request: {run.pull_request_url}")
    elif run.delivered_to:
        repository = run.code_snapshot.repository.name if run.code_snapshot_id else ""
        # The folder's path is the server's, not something to put in an answer.
        lines.append(
            "Delivered into the repository's folder on the server"
            + (f" ({repository})" if repository else "")
        )
    if plan and plan.reviewed_by_id:
        lines.append(
            f"Plan approved by: {person(plan.reviewed_by)}"
            + (f" on {plan.reviewed_at:%Y-%m-%d}" if plan.reviewed_at else "")
        )
    if run.graph_version:
        lines.append(f"Answered from knowledge graph version: {run.graph_version}")
    paths = [change.path for change in run.changes.all()]
    lines += ["", "Files changed:"] + ([f"- {path}" for path in paths] or ["- none recorded"])
    lines += ["", "Checks:"] + (
        [f"- {check.get('name', 'check')}: {check.get('conclusion')}" for check in run.checks]
        or ["- none reported"]
    )
    if plan:
        items = [item.title for item in plan.items.filter(status="accepted")]
        if items:
            lines += ["", "Accepted plan items, as the plan stated them:"]
            lines += [f'- "{item}"' for item in items]
    return title[:300], "\n".join(lines)


def incident_text(verdict):
    """(title, body) for a confirmed cause, from the verdict and what it names."""
    from .models import KnowledgeEntry

    hypothesis = verdict.hypothesis
    incident = hypothesis.run.incident
    cause = verdict.actual_cause
    title = f"Incident learning: {incident.title}"
    lines = [
        f"# {title}",
        "",
        "Type: Incident learning",
        f"Incident: {incident.title}",
        f"Confirmed cause: {cause.title}",
        f"Verdict: {verdict.get_verdict_display()}",
        f"Confirmed by: {person(verdict.user)}"
        + (f" on {verdict.confirmed_at:%Y-%m-%d}" if verdict.confirmed_at else ""),
        "",
        "The cause as triage proposed it:",
        f'"{hypothesis.statement}"',
    ]
    if verdict.note:
        lines += ["", f"Note from {person(verdict.user)}:", verdict.note]
    cited = list(dict.fromkeys(str(citation["id"]) for citation in hypothesis.citations))
    titles = {
        str(pk): name
        for pk, name in KnowledgeEntry.objects.filter(
            application_id=incident.application_id,
            pk__in=[key for key in map(_uuid, cited) if key],
        ).values_list("pk", "title")
    }
    found = [titles[key] for key in cited if key in titles]
    if found:
        lines += ["", "Evidence triage cited:"] + [f"- {name}" for name in found]
    return title[:300], "\n".join(lines)


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def withdraw(app):
    """Withdraw what lost the record it was built from. Returns how many."""
    from .models import KnowledgeEntry, LearningCandidate

    count = 0
    # A retracted verdict: the cause is no longer confirmed, so neither is
    # anything learned from it - an accepted learning is retired, not deleted.
    gone = LearningCandidate.objects.filter(
        application=app, kind="incident", verdict__isnull=True, state__in=["open", "accepted"]
    )
    for candidate in gone:
        if candidate.entry_id:
            KnowledgeEntry.objects.filter(pk=candidate.entry_id, active=True).update(active=False)
        candidate.state = "withdrawn"
        candidate.decision_note = "The verdict it was built from was retracted."
        candidate.save(update_fields=["state", "decision_note"])
        count += 1
    count += LearningCandidate.objects.filter(
        application=app, kind="change", run__isnull=True, state="open"
    ).update(state="withdrawn", decision_note="The run it was built from was removed.")
    # A removed conversation takes its question with it, whatever was decided:
    # the inbox must not keep what the asker or chat retention removed. A
    # benchmark question someone already made from it is theirs, and stays.
    count += LearningCandidate.objects.filter(
        application=app, kind__in=["gap", "wrong"], message__isnull=True
    ).exclude(question="", note="", body="").update(
        title="The conversation was removed",
        question="",
        note="",
        body="",
        expected="",
    )
    LearningCandidate.objects.filter(
        application=app, kind__in=["gap", "wrong"], message__isnull=True, state="open"
    ).update(state="withdrawn")
    return count


def _create(**fields):
    """One candidate, or nothing when another collection made it first."""
    from .models import LearningCandidate

    try:
        with transaction.atomic():
            return LearningCandidate.objects.create(**fields)
    except IntegrityError:
        return None


def collect(app):
    """Offer what this application's use produced since it was last read.

    Idempotent and bounded: each kind reads at most `BATCH` signals from the
    last `WINDOW_DAYS` that have no candidate yet. Returns how many it made.
    """
    from .models import ChatMessage, FactoryRun, LearningCandidate, TriageVerdict
    from .sensitivity import scan
    from .services import feature_enabled

    if not feature_enabled("knowledge", app):
        return 0
    withdraw(app)
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    made = 0

    runs = (
        FactoryRun.objects.filter(
            application=app,
            learning__isnull=True,
            created_at__gte=since,
            plan__status="approved",
            status__in=["delivered", "complete"],
        )
        .filter(Q(pull_request_url__gt="") | Q(delivered_to__gt=""))
        .select_related("plan__reviewed_by", "code_snapshot__repository")
        .order_by("-created_at")[:BATCH]
    )
    for run in runs:
        checks = run.checks or []
        # A suite still running is not a delivered fix yet, and a failing one
        # is not one at all; the first is read again next time.
        if any(not check.get("conclusion") for check in checks):
            continue
        if any(check["conclusion"] not in PASSING for check in checks):
            continue
        title, body = change_text(run)
        made += bool(
            _create(
                application=app, kind="change", run=run, title=title, body=body,
                signal_at=run.plan.reviewed_at or run.created_at,
            )
        )

    verdicts = (
        TriageVerdict.objects.filter(
            hypothesis__run__application=app,
            actual_cause__isnull=False,
            learning__isnull=True,
            created_at__gte=since,
        )
        .exclude(verdict="rejected")
        .select_related("user", "actual_cause", "hypothesis__run__incident")
        .order_by("-created_at")[:BATCH]
    )
    for verdict in verdicts:
        title, body = incident_text(verdict)
        made += bool(
            _create(
                application=app, kind="incident", verdict=verdict, title=title, body=body,
                signal_at=verdict.confirmed_at or verdict.created_at,
            )
        )

    asked = ChatMessage.objects.filter(
        conversation=OuterRef("conversation"), role="user", sequence=OuterRef("sequence") - 1
    ).values("body")[:1]
    unanswered = (
        ChatMessage.objects.filter(
            application=app,
            role="assistant",
            status="complete",
            citations=[],
            learning__isnull=True,
            created_at__gte=since,
        )
        .annotate(asked=Subquery(asked))
        .order_by("-created_at")[:BATCH]
    )
    seen = {
        question.casefold()
        for question in LearningCandidate.objects.filter(
            application=app, kind__in=["gap", "wrong"]
        ).exclude(question="").values_list("question", flat=True)
    }
    for message in unanswered:
        question = (message.asked or "").strip()[:500]
        kept = question and not scan(question) and question.casefold() not in seen
        # Recorded either way, so it is read once: a question that holds
        # personal data, or one already in the inbox, is kept without its text.
        made += bool(
            _create(
                application=app,
                kind="gap",
                message=message,
                state="open" if kept else "withdrawn",
                title=f"Nothing answered: {question[:200]}"
                if kept
                else "Not kept: personal data, or asked before",
                question=question if kept else "",
                signal_at=message.created_at,
            )
        )
        if kept:
            seen.add(question.casefold())
    return made


def collect_if_due():
    """The maintenance lane's step: collect for every live application, every
    `COLLECT_SECONDS`. The inbox also collects for its own application when it
    is drawn, so this is what keeps the Attention count true for everyone else."""
    global _last_collect
    from .models import Application
    from .policy import LIVE

    now = time.monotonic()
    if _last_collect is not None and now - _last_collect < COLLECT_SECONDS:
        return False
    _last_collect = now
    for app in Application.objects.filter(**LIVE).select_related("product__portfolio"):
        try:
            collect(app)
        except Exception:  # noqa: BLE001 - one application never stops the others
            logger.exception("learning_collect_failed", extra={"application": str(app.pk)})
    return True


class ExpectedForm(forms.Form):
    expected = forms.CharField(
        max_length=200,
        required=False,
        label="Should cite",
        help_text="Text in the expected source's title or link, such as a ticket key "
        "(KAN-4) or a document name (deployment-runbook).",
    )
    note = forms.CharField(max_length=1000, required=False, widget=forms.Textarea)

    def clean_expected(self):
        value = self.cleaned_data["expected"].strip()
        if value and len(value) < 3:
            raise forms.ValidationError("Use at least three characters, so it matches one source.")
        return value


def accept(user, app_id, candidate_id, expected="", note=""):
    """Make an open candidate knowledge, or a benchmark question. Returns it."""
    from .benchmark import MAX_QUESTIONS
    from .models import BenchmarkQuestion, LearningCandidate
    from .workbench import access, add_knowledge

    app, _ = access(user, app_id, "knowledge", write=True)
    organization = app.product.portfolio.organization
    with transaction.atomic():
        candidate = (
            LearningCandidate.objects.select_for_update()
            .filter(pk=candidate_id, application=app)
            .first()
        )
        if candidate is None:
            raise LearningCandidate.DoesNotExist
        if candidate.state != "open":
            raise ValidationError("This was already decided.")
        if candidate.kind in {"change", "incident"}:
            if not (candidate.run_id or candidate.verdict_id):
                raise ValidationError("The record this was built from is gone.")
            entry = add_knowledge(
                user, app.pk, candidate.title, candidate.body, source=source_for(candidate)
            )
            candidate.entry = entry
        else:
            expected = expected.strip()
            if len(expected) < 3:
                raise ValidationError(
                    "Name the source it should cite, so the benchmark can score it."
                )
            if not candidate.question:
                raise ValidationError("The question is no longer here.")
            if BenchmarkQuestion.objects.filter(application=app).count() >= MAX_QUESTIONS:
                raise ValidationError(f"An application can hold {MAX_QUESTIONS} questions.")
            question = BenchmarkQuestion.objects.create(
                application=app, question=candidate.question, expected=expected, created_by=user
            )
            audit(user, "benchmark.question_added", question.pk, organization)
            candidate.benchmark = question
            candidate.expected = expected
        candidate.state = "accepted"
        candidate.decided_by = user
        candidate.decided_at = timezone.now()
        candidate.decision_note = note.strip()[:1000]
        candidate.save()
        audit(
            user, "learning.accepted", candidate.pk, organization, details={"kind": candidate.kind}
        )
    return candidate


def reject(user, app_id, candidate_id, note=""):
    """Decline an open candidate. It stays, so the same signal is not offered again."""
    from .models import LearningCandidate
    from .workbench import access

    app, _ = access(user, app_id, "knowledge", write=True)
    with transaction.atomic():
        candidate = (
            LearningCandidate.objects.select_for_update()
            .filter(pk=candidate_id, application=app)
            .first()
        )
        if candidate is None:
            raise LearningCandidate.DoesNotExist
        if candidate.state != "open":
            raise ValidationError("This was already decided.")
        candidate.state = "rejected"
        candidate.decided_by = user
        candidate.decided_at = timezone.now()
        candidate.decision_note = note.strip()[:1000]
        candidate.save(update_fields=["state", "decided_by", "decided_at", "decision_note"])
        audit(
            user,
            "learning.rejected",
            candidate.pk,
            app.product.portfolio.organization,
            details={"kind": candidate.kind},
        )
    return candidate


def report(user, app_id, message_id, note="", expected=""):
    """Mark one of the user's own answers wrong. Makes or updates a candidate;
    never changes the answer, a source or the benchmark."""
    from .models import ChatMessage, LearningCandidate
    from .sensitivity import scan
    from .workbench import access

    app, _ = access(user, app_id, "chat")
    access(user, app_id, "knowledge")
    message = get_object_or_404(
        ChatMessage,
        pk=message_id,
        application=app,
        user=user,
        role="assistant",
        status="complete",
    )
    question = (
        ChatMessage.objects.filter(
            conversation=message.conversation, role="user", sequence=message.sequence - 1
        )
        .values_list("body", flat=True)
        .first()
        or ""
    ).strip()[:500]
    if not question:
        raise ValidationError("This answer has no question to report.")
    if scan(question) or scan(note):
        raise ValidationError(
            "The question or your note holds what looks like personal data, so it is not "
            "sent for review."
        )
    cited = [citation.get("title", "") for citation in message.citations if citation.get("title")]
    body = "Cited: " + "; ".join(dict.fromkeys(cited)) if cited else "Cited nothing."
    with transaction.atomic():
        candidate = (
            LearningCandidate.objects.select_for_update().filter(message=message).first()
        )
        if candidate is not None and candidate.state not in {"open", "withdrawn"}:
            raise ValidationError("This answer was already reviewed.")
        fields = {
            "kind": "wrong",
            "state": "open",
            "title": f"Marked wrong: {question[:200]}",
            "question": question,
            "note": note.strip()[:1000],
            "expected": expected.strip()[:200],
            "body": body[:4000],
            "reported_by": user,
        }
        if candidate is None:
            candidate = LearningCandidate.objects.create(
                application=app, message=message, signal_at=timezone.now(), **fields
            )
        else:
            for name, value in fields.items():
                setattr(candidate, name, value)
            candidate.save()
        audit(user, "chat.answer_reported", message.pk, app.product.portfolio.organization)
    return candidate


def open_count(app):
    from .models import LearningCandidate

    return LearningCandidate.objects.filter(application=app, state="open").count()


def learned_entries(app):
    """Active sources accepted from the inbox, for the Feed stage's count."""
    from .models import KnowledgeEntry

    return KnowledgeEntry.objects.filter(
        application=app, active=True, source__startswith=LEARNED_PREFIX
    ).count()


@login_required
@require_http_methods(["GET", "POST"])
def learned_view(request, pk):
    """Knowledge → Learned: the inbox. Owners and contributors only, because
    deciding what the application knows is theirs, and because it shows
    questions other people asked."""
    from .models import LearningCandidate
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge")
    if grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    error, failed = "", None
    if request.method == "POST":
        action = request.POST.get("action")
        candidate_id = _uuid(request.POST.get("candidate", ""))
        if candidate_id is None or action not in {"accept", "reject"}:
            raise PermissionDenied
        form = ExpectedForm(request.POST)
        try:
            if not form.is_valid():
                raise ValidationError([e for errors in form.errors.values() for e in errors])
            if action == "accept":
                done = accept(
                    request.user, pk, candidate_id,
                    form.cleaned_data["expected"], form.cleaned_data["note"],
                )
                messages.success(
                    request,
                    "Added to the benchmark. The next release check scores it."
                    if done.benchmark_id
                    else "Added as a source. The next version is built with it, and "
                    "is checked before it answers.",
                )
            else:
                reject(request.user, pk, candidate_id, form.cleaned_data["note"])
                messages.success(request, "Declined. It will not be offered again.")
            return redirect("learned", pk=pk)
        except LearningCandidate.DoesNotExist:
            raise PermissionDenied from None
        except ValidationError as refused:
            error, failed = " ".join(refused.messages), str(candidate_id)
    collect(app)
    candidates = LearningCandidate.objects.filter(application=app)
    return render(
        request,
        "learned.html",
        {
            "application": app,
            "grant": grant,
            "waiting": list(candidates.filter(state="open")[:100]),
            "decided": list(
                candidates.exclude(state="open")
                .select_related("decided_by", "entry", "benchmark")
                .order_by("-decided_at", "-signal_at")[:DECIDED_SHOWN]
            ),
            "error": error,
            "failed": failed,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def report_view(request, pk, message_id):
    """"This answer is wrong": a page of its own, so it works as a popup over chat
    and, with JavaScript off, as a page."""
    from .models import ChatMessage
    from .workbench import access

    app, _ = access(request.user, pk, "chat")
    access(request.user, pk, "knowledge")
    message = get_object_or_404(
        ChatMessage, pk=message_id, application=app, user=request.user, role="assistant"
    )
    back = f"{reverse('chat', args=[pk])}?conversation={message.conversation_id}"
    form = ExpectedForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        try:
            report(
                request.user, pk, message_id,
                form.cleaned_data["note"], form.cleaned_data["expected"],
            )
            messages.success(request, "Thanks. The application's owners will review it.")
            return redirect(back)
        except ValidationError as refused:
            error = " ".join(refused.messages)
    return render(
        request,
        "chat_report.html",
        {"application": app, "message": message, "form": form, "error": error, "back": back},
    )
