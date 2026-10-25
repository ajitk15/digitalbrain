"""Code Factory after approval: who may prepare, publish and refresh a run, and when.

Split out of `code_factory` along the line the pipeline already draws: the
first three phases produce a description of work and stop, and everything
after writes - or prepares to write - somebody's repository behind its own
gate. The shared machinery (phases, notes, `ask`, the ground rules) stays in
`code_factory`; nothing there imports from here.

This module is the gates and the queue: `gate` and `publish_gate`, the
preparation lane, `prepare` and `publish`, the stall and CI-checks sweeps and
the refresh after delivery. The agents it runs are `code_factory_agents`;
delivery into a folder instead of a pull request is `code_factory_local`, and
the before-and-after picture `code_factory_picture`. Each was part of this one
module, 2,700 lines, until Phase 5.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404
from django.utils import timezone

from .code_factory import PHASE_TIMEOUT, PHASE_TIMEOUTS, note
from .code_factory_agents import (
    REFERENCE_FILE_BYTES,
    local_repository,
    reference_files,
    run_delivery,
    run_implementation,
    run_review,
    run_tests_agent,
    run_verification,
    run_work_order,
)
from .models import FactoryRun, RunPhase
from .services import audit


def write_credential(app):
    """The write-scoped token, separate from the read-only connector one.

    Different files on purpose: letting an application import issues must not
    also let it push. Absent is a state, not a failure - an application with no
    write credential simply cannot deliver, and is told so.
    """
    from .secrets import application_secret

    return application_secret(app, "github_write")


def gate(user, app_id, run_id):
    """What writing a change needs: an approved plan, and somebody entitled.

    Deliberately does not require a repository or a credential. Writing the
    change reads files and produces new contents; it puts nothing anywhere. An
    application whose code this platform can read but must never write to - a
    client environment with no outbound GitHub write, most obviously - can run
    the agents and take the result away by hand, and refusing it a credential it
    does not need would be refusing it the whole product.

    `publish_gate` is where the repository and the token are required, because
    that is where something actually leaves.
    """
    from django.shortcuts import get_object_or_404

    from .workbench import access

    app, grant = access(user, app_id, "code_factory")
    if not grant.can_approve:
        raise PermissionDenied
    run = get_object_or_404(FactoryRun, pk=run_id, application=app)
    if run.plan is None or run.plan.status != "approved":
        raise ValidationError("Approve the plan before implementing it.")
    if run.pull_request_url:
        raise ValidationError("This run already opened a pull request.")
    # Absent is a state, not a failure: without it the agents read the pinned
    # code snapshot instead of the live repository. A run whose code came from a
    # folder on this server is never handed one: the folder is the repository,
    # and a GitHub token would send every read to a different one.
    if local_repository(run) is not None:
        return app, run, ""
    return app, run, write_credential(app)


def publish_gate(user, app_id, run_id):
    """What opening a pull request additionally needs.

    The repository is confirmed here and nowhere else, because this is the only
    step that writes: a ticket naming a repository is a guess, and whoever can
    file one must not get to choose where this platform writes.
    """
    app, run, token = gate(user, app_id, run_id)
    if not run.repository_confirmed:
        raise ValidationError(
            "Confirm the repository first. It was taken from the ticket, and a "
            "ticket does not get to choose where this platform writes."
        )
    if not token:
        raise ValidationError(
            f"Mount a write-scoped GitHub credential as github_write_{app.pk} in the "
            "configured secret directory. The read-only connector credential is "
            "deliberately not used for writing."
        )
    return app, run, token


def request_preparation(user, app_id, run_id):
    """Queue the implementation agents, rather than running them in the request.

    Build A runs on the worker and reports itself as it goes; this is the same
    work by the same measure - several model calls and a series of reads against
    somebody else's repository - so it belongs in the same place. Holding a
    request open for it would mean a blank page for a minute and no way to show
    what the agents are doing, which is the whole point of the screen.
    """
    app, run, _ = gate(user, app_id, run_id)
    claimed = FactoryRun.objects.filter(
        pk=run.pk, status__in=["awaiting_review", "prepared", "failed"]
    ).update(status="prepare_queued", error="", acting_user=user)
    if not claimed:
        raise ValidationError("This run is already working. Watch its progress.")
    run.refresh_from_db()
    note(
        run,
        f"Implementation requested by {user.get_username()}. Queued for the "
        "implementation agents; nothing is written to the repository by this.",
        level="check",
    )
    audit(
        user,
        "factory.change_requested",
        run.pk,
        app.product.portfolio.organization,
        details={"repository": run.proposed_repository},
    )
    return run


def process_next_preparation():
    """One queued implementation, for the worker's factory lane.

    Runs as whoever asked, and `gate` re-checks their grant inside this call -
    so approval withdrawn while it waited stops the work rather than being
    discovered afterwards.
    """
    run = (
        FactoryRun.objects.filter(status="prepare_queued")
        .select_related("acting_user")
        .order_by("created_at")
        .first()
    )
    if run is None or run.acting_user is None:
        return False
    from .tenancy import scoped

    try:
        with scoped(run.application_id):
            prepare(run.acting_user, run.application_id, run.pk, queued=True)
    except (ValidationError, PermissionDenied, Http404) as failure:
        reason = " ".join(getattr(failure, "messages", ["The request was refused."]))
        # Only a run still waiting: refused before its claim. One another
        # process claimed is that process's to finish, and one that failed
        # after its claim already says why.
        FactoryRun.objects.filter(pk=run.pk, status="prepare_queued").update(
            status="failed", error=reason[:2000]
        )
    return True


def prepare(user, app_id, run_id, queued=False):
    """Write the change and check it, without writing it anywhere.

    The first half of what used to be one act. Implementation and the pull
    request were a single call, so the contents existed only inside it and
    nobody saw what was about to be written until it had been. Stopping here -
    with the files produced, checked, and on screen - puts a person between
    deciding the work is right and letting it out.

    Nothing has left this platform when this returns. The branch does not
    exist, no commit has been made, and the run can be abandoned with no trace
    anywhere but its own record.
    """
    from .models import ProposedChange

    app, run, token = gate(user, app_id, run_id)
    # The lane takes only what is queued: a run another process prepared or
    # failed a moment ago is finished, not waiting - preparing it again would
    # pay for every agent twice. A person's button may start it from those.
    ready = ["prepare_queued"] if queued else [
        "prepare_queued", "awaiting_review", "prepared", "failed"
    ]
    claimed = FactoryRun.objects.filter(pk=run.pk, status__in=ready).update(
        status="preparing", error=""
    )
    if not claimed:
        raise ValidationError("This run is not ready to be implemented.")
    run.refresh_from_db()
    note(
        run,
        f"Implementation agents starting for {run.proposed_repository}. Nothing is "
        "written to the repository by this step.",
        level="check",
    )
    try:
        # The chain: what each file must do, the code, the tests that prove it,
        # a second model's reading of the result, then the mechanical checks.
        # Each is its own phase with its own receipt, so the screen can show
        # which agent produced what and which one refused.
        files, order = run_work_order(run, token)
        # One reference set for the whole chain. It was the implementation's
        # alone, so a live run's test author - never shown the consent service,
        # the audit module or the fixtures - wrote tests against functions and a
        # table that do not exist, and the review, shown none of it either,
        # could not tell.
        references = reference_files(run, files)
        changes = run_implementation(run, token, files, order=order, references=references)
        # A target the implementation left unchanged - the consent service it
        # calls, say - is still code the tests and the review must see. It was
        # a numbered file for the implementation; for them it is reference.
        changed = {change["path"] for change in changes}
        later = [
            {
                "path": item["path"],
                "text": item["text"][:REFERENCE_FILE_BYTES],
                "named": True,
                "test": False,
                "support": False,
            }
            for item in files
            if item["path"] not in changed and item["text"]
        ] + references
        # By path, not appended: when the plan names a test file the
        # implementation writes it too, and the test author's version - built on
        # the implementation's - replaces it rather than becoming a second
        # change to the same file.
        written = {
            change["path"]: change
            for change in run_tests_agent(run, changes, token, references=later)
        }
        changes = [written.pop(change["path"], change) for change in changes] + list(
            written.values()
        )
        changes = run_review(run, changes, order, references=later)
        run_verification(run, changes, token)
    except ValidationError as failure:
        note(run, "Implementation stopped. Nothing was written.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error=" ".join(failure.messages)[:2000]
        )
        raise
    with transaction.atomic():
        # Replaced wholesale: a second attempt supersedes the first rather than
        # merging with it, so what is on screen is always one implementation.
        ProposedChange.objects.filter(run=run).delete()
        ProposedChange.objects.bulk_create(
            ProposedChange(
                run=run,
                path=change["path"],
                content=change["content"],
                base_sha=change["sha"] or "",
            )
            for change in changes
        )
        FactoryRun.objects.filter(pk=run.pk).update(status="prepared")
    audit(
        user,
        "factory.change_prepared",
        run.pk,
        app.product.portfolio.organization,
        details={"repository": run.proposed_repository, "files": len(changes)},
    )
    note(
        run,
        f"{len(changes)} file(s) are ready. Read the summary and decide whether to "
        + ("write them to the folder" if local_repository(run) else "open a pull request")
        + f". Nothing has been written to {run.proposed_repository} yet.",
        level="result",
    )
    return changes


def publish(user, app_id, run_id):
    """Open the pull request for a change somebody has already looked at.

    Deliberately re-reads the prepared files rather than taking them from the
    caller: what gets written is what was shown, and a request cannot smuggle
    in contents nobody reviewed.
    """
    app, run, token = publish_gate(user, app_id, run_id)
    if run.status != "prepared":
        raise ValidationError("Implement the change and read its summary first.")
    changes = [
        {"path": item.path, "content": item.content, "sha": item.base_sha or None}
        for item in run.changes.all()
    ]
    if not changes:
        raise ValidationError("This run has no prepared change to open.")
    FactoryRun.objects.filter(pk=run.pk).update(status="delivering", error="")
    run.refresh_from_db()
    note(run, f"Pull request approved by {user.get_username()}.", level="check")
    try:
        # Re-checked immediately before writing, not when it was prepared: the
        # repository may have moved on while the summary was being read, and
        # that is exactly the window this check exists for.
        run_verification(run, changes, token)
        url = run_delivery(run, changes, token)
    except ValidationError as failure:
        note(run, "Delivery stopped.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error=" ".join(failure.messages)[:2000]
        )
        raise
    audit(
        user,
        "factory.pull_request_opened",
        run.pk,
        app.product.portfolio.organization,
        details={"repository": run.proposed_repository, "url": url, "files": len(changes)},
    )
    FactoryRun.objects.filter(pk=run.pk).update(status="delivered", finished_at=timezone.now())
    return url


#: How long a working run may say nothing before it is presumed dead.
#:
#: A phase writes a line before each model call and another after it, so silence
#: means one call in flight - bounded by the longest phase timeout. Three times
#: that leaves room for a slow provider and still catches the case this exists
#: for: the worker process went away. A restart does that, and every run it had
#: claimed stays "Running" forever with nobody left to finish it. Measured from
#: the longest, or a healthy implementation call would be reclaimed mid-answer.
STALL_AFTER = timedelta(seconds=max(PHASE_TIMEOUT, *PHASE_TIMEOUTS.values()) * 3)

#: The statuses a worker holds a run in. Nothing else can stall: a run waiting
#: for a person is waiting on purpose.
WORKING = ("running", "preparing")


def last_sign_of_life(run):
    event = run.events.all().last()
    return event.at if event else run.created_at


def reclaim_stalled_runs():
    """Fail runs whose worker is gone, rather than leaving them "Running".

    Failed rather than re-queued. Re-queueing would spend the provider budget
    again on work that may in fact have completed and simply not been recorded,
    and the pipeline's own answer to a failed run is to start another - which a
    person can do, knowing what it costs.

    Reading the last event rather than a claimed-at timestamp means the clock
    measures progress rather than wall time: a run that is slow but talking is
    not stalled.
    """
    cutoff = timezone.now() - STALL_AFTER
    stalled = (
        FactoryRun.objects.filter(status__in=WORKING)
        .annotate(spoke_at=Max("events__at"))
        .filter(Q(spoke_at__lt=cutoff) | Q(spoke_at__isnull=True, created_at__lt=cutoff))
    )
    for run in stalled:
        quiet = int((timezone.now() - last_sign_of_life(run)).total_seconds() // 60)
        reason = (
            f"No progress for {quiet} minutes. The worker that claimed this run "
            "is gone - most often because the server restarted while it was "
            "working. Nothing was written anywhere. Run the ticket again."
        )
        claimed = FactoryRun.objects.filter(pk=run.pk, status=run.status).update(
            status="failed", error=reason, finished_at=timezone.now()
        )
        if claimed:
            note(run, reason, level="problem")
            RunPhase.objects.filter(run=run, status="running").update(
                status="failed",
                error="Abandoned: the worker went away.",
                finished_at=timezone.now(),
            )
    return bool(stalled)


#: How often a delivered run asks GitHub about its checks. A test suite takes
#: minutes, and asking every tick would spend the rate limit on hearing "still
#: running".
CHECKS_INTERVAL = 30


def refresh_checks(run):
    """Ask GitHub what its checks made of this run's branch.

    Read as the application, with its read credential rather than the write one:
    nothing here writes, and a token that may open pull requests should not be
    spent on polling.
    """
    from .github_write import check_runs
    from .link_sources import github_token

    token = github_token(run.application) or write_credential(run.application)
    if not token or not run.branch:
        return False
    try:
        found = check_runs(run.proposed_repository, run.branch, token)
    except ValidationError:
        # A repository with no checks, or one that cannot be read right now, is
        # not a failure of the run: the pull request is open either way.
        FactoryRun.objects.filter(pk=run.pk).update(checks_read_at=timezone.now())
        return False
    before = {entry["name"]: entry.get("conclusion") for entry in run.checks or []}
    FactoryRun.objects.filter(pk=run.pk).update(
        checks=found, checks_read_at=timezone.now()
    )
    for entry in found:
        was = before.get(entry["name"])
        if entry.get("conclusion") and entry["conclusion"] != was:
            note(
                run,
                f"{entry['name']}: {entry['conclusion']}.",
                phase="delivery",
                level="result" if entry["conclusion"] == "success" else "problem",
            )
    return True


def process_next_checks():
    """One delivered run whose checks are worth asking about again.

    Stops asking once every check has a conclusion: a finished suite does not
    change, and a run whose pull request is merged or closed elsewhere is not
    this platform's business.
    """
    from django.db.models import Q

    due = timezone.now() - timedelta(seconds=CHECKS_INTERVAL)
    run = (
        FactoryRun.objects.filter(status="delivered")
        .exclude(branch="")
        .filter(Q(checks_read_at__isnull=True) | Q(checks_read_at__lt=due))
        .select_related("application")
        .order_by("checks_read_at")
        .first()
    )
    if run is None:
        return False
    # Claimed by moving its turn on, so two processes never ask GitHub together.
    if not FactoryRun.objects.filter(pk=run.pk, checks_read_at=run.checks_read_at).update(
        checks_read_at=timezone.now()
    ):
        return True
    if run.checks and all(entry.get("conclusion") for entry in run.checks):
        # Settled. Touch the timestamp so it falls to the back of the queue
        # rather than being asked about forever.
        FactoryRun.objects.filter(pk=run.pk).update(checks_read_at=timezone.now())
        return False
    from .tenancy import scoped

    with scoped(run.application_id):
        return refresh_checks(run)


#: What a finished run can offer to bring back in line with the code, and what
#: each one actually costs. Ordered cheapest and least disruptive first.
#:
#: Every one of these already has a screen of its own with its own permission
#: check and its own audit event. Nothing here is a second way to do any of
#: them - each entry calls the same function that screen calls, so a person
#: who may not re-index from Code Graph cannot re-index from here either.
REFRESH_TARGETS = (
    (
        "documents",
        "Documents",
        "Re-read every linked source and bring down anything that changed at "
        "the origin. No model runs and nothing is charged.",
    ),
    (
        "knowledge",
        "Knowledge graph",
        "Queue a structural rebuild. Free, and it produces a draft - what runs "
        "and chat answer from is the published revision, which stays as it is "
        "until somebody publishes the new one.",
    ),
    (
        "code",
        "Code Graph",
        "Re-index the repository from its default branch, which supersedes the "
        "snapshot for future runs. Past runs keep the snapshot they reasoned "
        "about.",
    ),
)

REFRESH_KEYS = tuple(key for key, _, _ in REFRESH_TARGETS)


def refresh_gate(user, app_id, run_id):
    """A finished run, and somebody who may change this application's knowledge.

    Not `gate`: refreshing is maintenance rather than a decision about a change,
    so it wants write access rather than approval rights. The delicate part is
    delegated anyway - each target calls the screen's own entry point, which
    re-checks the feature and the role for itself.
    """
    from django.shortcuts import get_object_or_404

    from .workbench import access

    app, grant = access(user, app_id, "code_factory", write=True)
    run = get_object_or_404(FactoryRun, pk=run_id, application=app)
    if not run.delivered:
        raise ValidationError(
            "There is nothing to refresh against yet: this run has not delivered "
            "its change."
        )
    if run.refresh_choice:
        raise ValidationError("That question has already been answered for this run.")
    return app, run


def decline_refresh(user, app_id, run_id):
    """Record that somebody looked at the question and said no.

    A real answer, stored like any other. Without it the stage would go on
    asking, and "nobody has decided" would be indistinguishable from "somebody
    decided not to" - which is the difference between an outstanding task and a
    finished one.
    """
    app, run = refresh_gate(user, app_id, run_id)
    FactoryRun.objects.filter(pk=run.pk, refresh_choice="").update(
        refresh_choice="declined", refresh_scope=[], refresh_at=timezone.now(),
        refresh_by=user,
    )
    audit(user, "factory.refresh_declined", run.pk, app.product.portfolio.organization)
    note(
        run,
        f"{user.get_username()} chose to leave this application's documents and "
        "graphs as they are. Nothing was rebuilt.",
        level="result",
    )
    return run


def refresh_after(user, app_id, run_id, scope):
    """Bring the chosen parts of this application's knowledge up to date.

    The question this answers is the one nobody remembers to ask: the change is
    written and tested, and every description this application holds of that
    code is now describing it as it was. The graph a later run cites, the
    documents chat answers from, the snapshot the design reads - all of them
    still say what was true before the run that just finished.

    Offered rather than done. Each of these supersedes something a past run
    reasoned about, and the knowledge graph can cost money, so the decision
    belongs to a person the same way publishing a revision does.

    Returns the notes to put in front of them, in order.
    """
    from .code_graph_ingest import valid_name
    from .graphs import claim_generation
    from .knowledge_sources import resync
    from .models import CodeRepository, KnowledgeSource
    from .services import feature_enabled

    app, run = refresh_gate(user, app_id, run_id)
    chosen = [key for key in REFRESH_KEYS if key in set(scope or ())]
    if not chosen:
        raise ValidationError("Choose at least one thing to refresh, or say no.")

    notes = []
    if "documents" in chosen:
        # A folder upload has no origin to re-read - `resync` refuses it - and
        # one refusal used to abort the whole refresh, code graph included.
        uploaded = KnowledgeSource.objects.filter(application=app, provider="folder")
        if uploaded.exists():
            notes.append(
                f"{uploaded.count()} uploaded folder(s) have no origin to re-read; "
                "upload them again to bring them up to date."
            )
        sources = list(KnowledgeSource.objects.filter(application=app).exclude(provider="folder"))
        if not sources:
            notes.append("No linked sources to re-read; documents were left alone.")
        else:
            queued = orphaned = 0
            for source in sources:
                added, missing = resync(user, app.pk, source.pk)
                queued += added
                orphaned += missing
            notes.append(
                f"Re-read {len(sources)} source(s): {queued} document(s) queued for "
                f"download"
                + (f", {orphaned} no longer at the origin." if orphaned else ".")
            )

    if "knowledge" in chosen:
        claimed, refusal = claim_generation(user, app)
        notes.append(
            "Structural graph generation queued. It produces a draft; publish it "
            "when you are happy with it, and until you do, runs and chat keep "
            "answering from the revision they answer from now."
            if claimed
            else refusal
        )

    if "code" in chosen:
        # The folder this run wrote to is the repository that changed; a GitHub
        # run re-indexes the first registered GitHub repository, as it always has.
        if run.delivered_to:
            repository = local_repository(run)
            if repository is not None and repository.retired_at is not None:
                repository = None
        else:
            repository = (
                CodeRepository.objects.filter(
                    application=app, provider="github", retired_at__isnull=True
                )
                .exclude(status="documentation")
                .order_by("created_at")
                .first()
            )
        if not feature_enabled("code_graph", app):
            notes.append("Code Graph is switched off for this application.")
        elif repository is None:
            notes.append("No repository is registered on Code Repos.")
        else:
            if repository.provider == "github":
                valid_name(repository.name)
            CodeRepository.objects.filter(pk=repository.pk).update(
                status="queued",
                error="",
                added_by=user,
                job_id=uuid.uuid4(),
                updated_at=timezone.now(),
            )
            audit(
                user,
                "code_repository.queued",
                repository.pk,
                app.product.portfolio.organization,
                {"repository": repository.name, "run": run.number},
            )
            origin = "the folder" if repository.provider == "local" else repository.default_ref
            notes.append(
                f"{repository.name} queued for re-indexing from {origin}. The current "
                "snapshot stays readable until the new one is ready."
            )

    FactoryRun.objects.filter(pk=run.pk, refresh_choice="").update(
        refresh_choice="queued",
        refresh_scope=chosen,
        refresh_at=timezone.now(),
        refresh_by=user,
    )
    audit(
        user,
        "factory.refresh_queued",
        run.pk,
        app.product.portfolio.organization,
        {"scope": chosen},
    )
    labels = ", ".join(
        label.lower() for key, label, _ in REFRESH_TARGETS if key in chosen
    )
    note(
        run,
        f"{user.get_username()} asked for {labels} to be brought up to date "
        "after this change.",
        level="result",
    )
    for line in notes:
        note(run, line)
    return notes


def discard(user, app_id, run_id):
    """Throw away a prepared change without opening anything.

    The other answer to "shall I open a pull request?", and it has to be a real
    action rather than closing the tab: the contents are held on this platform
    until somebody says what to do with them.
    """
    from .models import ProposedChange

    app, run, _ = gate(user, app_id, run_id)
    if run.status != "prepared":
        raise ValidationError("There is no prepared change to discard.")
    with transaction.atomic():
        ProposedChange.objects.filter(run=run).delete()
        FactoryRun.objects.filter(pk=run.pk).update(
            status="awaiting_review", finished_at=timezone.now()
        )
    audit(user, "factory.change_discarded", run.pk, app.product.portfolio.organization)
    note(
        run,
        f"The prepared change was discarded by {user.get_username()}. Nothing was "
        "written to the repository. The approved plan is unchanged and can be "
        "implemented again.",
        level="result",
    )
    return run
