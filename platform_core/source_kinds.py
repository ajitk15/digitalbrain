"""The kinds of source Knowledge reads, as one registry.

Documents, code repositories and tickets each had their own listing code and
their own hand-written tab, so a new kind meant a new page *and* new navigation
code, and the onboarding cycle had to know every kind by name. This is the same
pattern as `services.FEATURES`, `ontology.PACKS` and `connector_kinds.KINDS`: one
closed registry, read by the Knowledge tabs (`workspace.source_tabs`) and the
cycle's Feed stage (`lifecycle.stages`). Adding a kind is one entry here plus
its own page; nothing that draws Knowledge needs to change.

It is a way of reading, never of storing: `Document`/`KnowledgeEntry`,
`CodeRepository` and connector records stay exactly where they are, and each
kind's page keeps its own queries, rules and permission checks.

**Code is a kind of source, not a kind of knowledge** (`feeds_graph=False`): it
is read by Code Graph, never by the knowledge graph, so the cycle counts it
apart and nothing here may make the graph read it.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Kind:
    key: str
    #: The tab's label.
    label: str
    #: (one, many), for a count: "1 document", "3 repositories".
    nouns: tuple
    #: The feature that switches the kind on. Each kind is gated on its own, so
    #: an application with only one of them still reaches what it has.
    feature: str
    icon: str
    #: Its page, a url name taking the application id.
    route: str
    #: The `knowledge_view` values its tab is current on: a kind's page and the
    #: pages that read one of its rows.
    views: tuple
    #: Whether the knowledge graph reads it.
    feeds_graph: bool = True
    #: How many it holds for an application, or None when the kind has nothing
    #: to show there (Tickets with no ticket connector); None hides the tab.
    count: object = field(default=None, compare=False)
    #: Whether its tab shows, beyond the feature: None means always.
    shown: object = field(default=None, compare=False)

    def visible(self, app):
        from .services import feature_enabled

        if not feature_enabled(self.feature, app):
            return False
        return self.shown is None or self.shown(app)

    def counted(self, count):
        return f"{count} {self.nouns[0] if count == 1 else self.nouns[1]}"


def _documents(app):
    from .models import KnowledgeEntry

    return KnowledgeEntry.objects.filter(
        application=app, active=True, document__isnull=False
    ).count()


def _code(app):
    from .models import CodeRepository

    return (
        CodeRepository.objects.filter(application=app, retired_at__isnull=True)
        .exclude(status="documentation")
        .count()
    )


def _tickets(app):
    from .ticket_library import tab_count

    return tab_count(app)


def _has_tickets(app):
    # The Tickets tab shows only when the application imports tickets; the tab
    # carries no count, which read as a problem whenever it was 0.
    return _tickets(app) is not None


#: In the order the tabs show them, before Graph, Quality and Versions.
KINDS = {
    "documents": Kind(
        "documents",
        "Documents",
        ("document", "documents"),
        "knowledge",
        "document",
        "documents",
        ("sources", "documents"),
        count=_documents,
    ),
    "code": Kind(
        "code",
        "Code Repos",
        ("repository", "repositories"),
        "code_graph",
        "code-graph",
        "code-repositories",
        ("code",),
        feeds_graph=False,
        count=_code,
    ),
    "tickets": Kind(
        "tickets",
        "Tickets",
        ("ticket", "tickets"),
        "knowledge",
        "audit",
        "tickets",
        ("tickets",),
        count=_tickets,
        shown=_has_tickets,
    ),
}


def visible(app):
    """The kinds this application's Knowledge shows, in tab order."""
    return [kind for kind in KINDS.values() if kind.visible(app)]


def fed(app):
    """[(kind, count)] for the cycle's Feed stage: every visible kind holding something."""
    found = []
    for kind in visible(app):
        count = kind.count(app) if kind.count else None
        if count:
            found.append((kind, count))
    return found
