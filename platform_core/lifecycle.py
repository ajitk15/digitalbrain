"""Where an application's knowledge stands: set up, fed, built, checked, published, used.

The cycle existed before this module did - sources go in, a version is built,
someone publishes it, chat and Code Factory answer from it - but nothing showed
it as one path. Onboarding was organised by Code Factory's gates, the Graph page
said "N sources changed since" only to someone who opened it, and a draft ready
to publish waited until somebody happened to look at Versions.

So this answers, from records the platform already keeps and with no model:

* `stages` - the six stages, each done, waiting or not started, for onboarding;
* `summary` - one line for every Knowledge page: what answers, what is waiting,
  what is behind;
* `attention` - the three things that wait on a person: a draft ready to
  publish, a published version behind its sources, sources that moved at their
  origin.

Read-only. Every stage links to the screen that acts on it, which keeps its own
permission check and audit event: this page never becomes a second way to do
anything.
"""

from dataclasses import dataclass

from django.db.models import Count, F, OuterRef, Q, Subquery

#: The stages in the order the cycle runs them. Learn and Keep current are not
#: stages a person ticks off: they are the cycle running again, which is what
#: `summary` and the Attention items are for.
STAGES = (
    ("setup", "Set up"),
    ("feed", "Feed"),
    ("build", "Build"),
    ("check", "Check"),
    ("publish", "Publish"),
    ("use", "Use"),
)

#: A stage's state. "warn" is something to look at, never a failure: a draft
#: that answers fewer benchmark questions may still be the right one.
DONE, WARN, TODO = "done", "warn", "todo"


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    state: str
    detail: str
    #: A url name taking the application id, and what follows it.
    route: str
    query: str = ""


@dataclass(frozen=True)
class Summary:
    """What one line on a Knowledge page says. Numbers only, nothing written by a person."""

    published: object
    latest: object
    #: The newest version, when it is newer than the published one and still
    #: reads its sources exactly: something a person can publish now.
    draft: object
    #: The published version no longer matches the sources and settings a
    #: build would read, and no newer version does either.
    behind: bool
    #: Sources whose origin has moved on since they were read.
    moved: int
    moved_code: int


def knowledge_count(app):
    """How many active sources feed the graph, of every kind."""
    from .models import KnowledgeEntry

    return KnowledgeEntry.objects.filter(application=app, active=True).count()


def moved(app):
    """(sources, repositories) whose origin has moved on since they were read, in one query.

    The same signals the Documents and Code Repos pages show: a link or folder
    origin marked out of sync by its scheduled check, and a repository whose
    branch now points past its latest snapshot. Detection only; nothing is
    re-read until a person asks.
    """
    from .models import Application, CodeRepository, CodeSnapshot, KnowledgeSource
    from .services import feature_enabled

    def counted(queryset):
        return Subquery(
            queryset.order_by().values("application").annotate(n=Count("pk")).values("n")[:1]
        )

    stale = KnowledgeSource.objects.filter(
        application=OuterRef("pk"), status=KnowledgeSource.Status.STALE
    )
    latest = CodeSnapshot.objects.filter(repository=OuterRef("pk")).order_by("-number")
    drifted = (
        CodeRepository.objects.filter(application=OuterRef("pk"), retired_at__isnull=True)
        .exclude(head_sha="")
        .annotate(indexed=Subquery(latest.values("commit_sha")[:1]))
        .exclude(indexed__isnull=True)
        .exclude(indexed="")
        .exclude(indexed=F("head_sha"))
    )
    counts = (
        Application.objects.filter(pk=app.pk)
        .annotate(stale=counted(stale), drifted=counted(drifted))
        .values("stale", "drifted")
        .first()
        or {}
    )
    code = (counts.get("drifted") or 0) if feature_enabled("code_graph", app) else 0
    return counts.get("stale") or 0, code


def summary(app):
    """Where the published version stands against what would be built now.

    Four queries: the published and newest versions together, the two the
    fingerprint reads (sources and the AI setting; the ontology is the
    application's own), and what moved at its origin.
    """
    from .graphs import fingerprint
    from .models import GraphRevision
    from .ontology import CORE, PACKS

    revisions = GraphRevision.objects.filter(application=app).defer("data")
    published_pk = (
        revisions.filter(published_at__isnull=False)
        .order_by("-published_at", "-number")
        .values("pk")[:1]
    )
    latest_pk = revisions.order_by("-number").values("pk")[:1]
    found = {
        revision.pk: revision
        for revision in revisions.filter(
            Q(pk__in=Subquery(published_pk)) | Q(pk__in=Subquery(latest_pk))
        )
    }
    ordered = sorted(found.values(), key=lambda revision: revision.number)
    published = max(
        (revision for revision in ordered if revision.published_at),
        key=lambda revision: (revision.published_at, revision.number),
        default=None,
    )
    latest = ordered[-1] if ordered else None
    current = fingerprint(app.pk, PACKS.get(app.ontology or "", CORE)) if latest else ""
    draft = (
        latest
        if latest
        and latest.fingerprint == current
        and (published is None or latest.number > published.number)
        else None
    )
    behind = bool(published and draft is None and published.fingerprint != current)
    stale, code = moved(app)
    return Summary(published, latest, draft, behind, stale, code)


def stages(app, state=None):
    """The six stages for this application, each with where to act on it."""
    from .benchmark import latest_runs
    from .models import KnowledgeGraph
    from .ontology import for_application
    from .readiness import chosen_connectors
    from .services import feature_enabled, knowledge_label, purposes

    state = state or summary(app)
    total = knowledge_count(app)
    label = knowledge_label(app)
    found = []

    chosen = chosen_connectors(app)
    domain = for_application(app.pk)
    found.append(
        Stage(
            "setup",
            "Set up",
            DONE if chosen is not None else TODO,
            f"{', '.join(p.title() for p in purposes(app)) or 'Knowledge'} · "
            f"{domain.label} ontology"
            + ("" if chosen is not None else " · connectors not chosen yet"),
            # Features, not the connector question: onboarding already offers
            # that as its next step, and offering it twice is what its card ended.
            "application-features",
        )
    )

    # Each kind of source from the registry (`source_kinds`), then whatever
    # feeds the graph that no kind lists: notes, articles and other imports.
    from .source_kinds import fed as by_kind

    counted = by_kind(app)
    in_kinds = sum(count for kind, count in counted if kind.feeds_graph)
    named = [kind.counted(count) for kind, count in counted]
    other = total - in_kinds
    if other > 0:
        named.append(f"{other} other record{'s' if other != 1 else ''}")
    found.append(
        Stage(
            "feed",
            "Feed",
            DONE if total else TODO,
            ", ".join(named) if named else "No sources yet.",
            "documents",
        )
    )

    graph = KnowledgeGraph.objects.filter(application=app).only("status").first()
    latest = state.latest
    building = graph is not None and graph.status in {"queued", "building"}
    failed = graph is not None and graph.status == "failed"
    found.append(
        Stage(
            "build",
            "Build",
            WARN if failed else DONE if latest else TODO,
            "The last build failed."
            if failed
            else (f"Version {latest.number}" + (" · building the next" if building else ""))
            if latest
            else "Building the first version."
            if building
            else "No version built yet.",
            "graph",
        )
    )

    scores = latest_runs(app, [r for r in (latest, state.published) if r]) if latest else {}
    newest = scores.get(latest.pk) if latest else None
    answering = scores.get(state.published.pk) if state.published else None
    # A newer version's release check says it best (`release.check`, stored
    # when the version was saved); the benchmark alone is the fallback for a
    # version checked under other rules, or never.
    released = latest.release_state if latest and latest != state.published else None
    if released:
        failed = [item["label"].lower() for item in latest.release["checks"] if not item["ok"]]
        against = latest.release.get("against")
        check = Stage(
            "check",
            "Check",
            DONE if released == "passed" else WARN,
            f"Version {latest.number} passed its release check"
            + (f" against version {against}." if against else ".")
            if released == "passed"
            else f"Version {latest.number} cannot be published: {', '.join(failed)}."
            if released == "refused"
            else f"Version {latest.number} did worse than version {against} on "
            f"{', '.join(failed)}.",
            "graph",
            "?tab=versions",
        )
    elif newest is None:
        check = Stage(
            "check",
            "Check",
            TODO,
            "No benchmark questions scored yet." if latest else "Nothing to check yet.",
            "graph",
            "?tab=quality",
        )
    elif answering and latest.pk != state.published.pk and newest.found < answering.found:
        check = Stage(
            "check",
            "Check",
            WARN,
            f"Version {latest.number} finds {newest.found} of {newest.questions} "
            f"benchmark questions; the published version found {answering.found}.",
            "graph",
            f"?tab=quality&version={latest.number}",
        )
    else:
        check = Stage(
            "check",
            "Check",
            DONE,
            f"Version {latest.number} finds {newest.found} of {newest.questions} "
            "benchmark questions.",
            "graph",
            f"?tab=quality&version={latest.number}",
        )
    found.append(check)

    published = state.published
    found.append(
        Stage(
            "publish",
            "Publish",
            WARN
            if published and (state.draft or state.behind)
            else DONE
            if published
            else TODO,
            (
                f"Version {published.number} answers"
                + (
                    f" · version {state.draft.number} is ready to publish"
                    if state.draft
                    else " · behind its sources"
                    if state.behind
                    else ""
                )
            )
            if published
            else f"Version {state.draft.number} is ready to publish."
            if state.draft
            else "Nothing published: chat and Code Factory have nothing to answer from.",
            "graph",
            "?tab=versions",
        )
    )

    consumers = [
        name
        for key, name in (
            ("chat", "Chat"),
            ("code_factory", "Code Factory"),
            ("service_ops", "ServiceOps"),
            ("chat_api", "the API"),
        )
        if feature_enabled(key, app)
    ]
    found.append(
        Stage(
            "use",
            "Use",
            DONE if published and consumers else TODO,
            (
                f"{listed(consumers)} answer from version {published.number}."
                if published
                else f"{listed(consumers)} will answer once a version is published."
            )
            if consumers
            else f"Nothing reads {label} yet.",
            "onboarding",
        )
    )
    return found


def listed(names):
    """"Chat, Code Factory and the API": a list as a sentence says it."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def attention(app, grant):
    """What waits on this person in this application's knowledge, as attention items.

    Only for someone who can act on it: publishing and re-reading sources are
    owner and contributor actions, so a viewer is told nothing they could not do.
    """
    from django.urls import reverse

    from .attention import Item
    from .services import feature_enabled

    if grant is None or grant.role not in {"owner", "contributor"}:
        return []
    if not feature_enabled("knowledge", app):
        return []
    state = summary(app)
    found = []
    graph = reverse("graph", args=[app.pk])
    if state.draft:
        released = state.draft.release_state
        found.append(
            Item(
                app,
                f"Version {state.draft.number} "
                + {
                    "passed": "passed its release check and is waiting to be published",
                    "warnings": "is waiting to be published; its release check has warnings",
                    "refused": "was refused by its release check",
                }.get(released, "is built and waiting to be published"),
                reverse("graph-release", args=[app.pk, state.draft.number]),
                "network",
                "See why" if released == "refused" else "Review it",
                tone="problem" if released == "refused" else "attention",
                kind="knowledge",
            )
        )
    elif state.behind:
        found.append(
            Item(
                app,
                f"The published version {state.published.number} is behind its sources",
                graph,
                "network",
                "See what changed",
                kind="knowledge",
            )
        )
    if state.moved:
        found.append(
            Item(
                app,
                f"{state.moved} source{'s' if state.moved != 1 else ''} moved at "
                f"{'their' if state.moved != 1 else 'its'} origin",
                reverse("documents", args=[app.pk]),
                "refresh",
                "Resync",
                kind="knowledge",
            )
        )
    if state.moved_code:
        found.append(
            Item(
                app,
                f"{state.moved_code} repositor{'ies' if state.moved_code != 1 else 'y'}"
                " ahead of the indexed code",
                reverse("code-repositories", args=[app.pk]),
                "code-graph",
                "Refresh",
                kind="knowledge",
            )
        )
    return found
