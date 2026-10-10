"""The knowledge graph's ontology: what kinds of things it holds, and how they relate.

AI extraction used to write whatever relation the model chose, so one graph
said "uses", "relies on" and "depends on" for the same fact, and an entity had
no type at all. Two things followed: a path from a ticket to the requirement it
breaks was a different path every run, and nothing could tell a component from
a person with the same name.

This is a closed registry, the same pattern as `services.FEATURES` and the
connector `KINDS`: eight types, and the relations allowed between them. The
extraction prompt is written from it and the validator enforces it, so they
cannot drift. A relationship outside it is rejected the way an unverified quote
is - typing is a filter, never evidence: every typed relationship still needs
its exact quote in an active source.

The Component and Code types are the ones the operations and code graphs
already use, which is what lets a path cross from a ticket into code and into
operations.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

#: Saved on every revision an AI run produced. Bump it when a type or relation
#: changes meaning, so a version can say which list it was extracted under.
ONTOLOGY_VERSION = 1

#: Type -> what it is, as the model is told. Kept small on purpose: extend it
#: only when a benchmark question needs a type it does not have.
TYPES = {
    "Ticket": "a bug, story or task, usually with a key such as CARE-401",
    "Requirement": "a functional or non-functional requirement or business rule",
    "Control": "a control, regulation, policy or standard clause",
    "Component": "a service, system, API, database, queue or named deployment",
    "Code": "a source file, module, class or function",
    "Runbook": "a runbook, procedure or knowledge article",
    "Incident": "an outage or incident",
    "Change": "a release, deployment event or change request",
}

#: Other names a model uses for a type. Matched after case-folding.
TYPE_ALIASES = {
    "bug": "Ticket",
    "story": "Ticket",
    "task": "Ticket",
    "issue": "Ticket",
    "nfr": "Requirement",
    "business rule": "Requirement",
    "regulation": "Control",
    "policy": "Control",
    "standard": "Control",
    "service": "Component",
    "system": "Component",
    "api": "Component",
    "database": "Component",
    "file": "Code",
    "module": "Code",
    "class": "Code",
    "function": "Code",
    "procedure": "Runbook",
    "knowledge article": "Runbook",
    "kb article": "Runbook",
    "outage": "Incident",
    "release": "Change",
    "change request": "Change",
}

ANY = frozenset(TYPES)


@dataclass(frozen=True)
class Relation:
    subjects: frozenset
    objects: frozenset
    #: Phrasings that mean this relation, in the same direction.
    aliases: tuple = ()


def _types(*names):
    return frozenset(names)


#: Canonical relation -> which types may sit at each end. `references` is the
#: catch-all: it keeps a relationship the others do not fit, under one name.
RELATIONS = {
    "depends on": Relation(
        _types("Component", "Code"),
        _types("Component", "Code"),
        (
            "uses",
            "relies on",
            "requires",
            "calls",
            "consumes",
            "connects to",
            "reads from",
            "writes to",
            "sends messages to",
            "sends to",
            "is dependent on",
        ),
    ),
    "affects": Relation(
        _types("Ticket", "Incident"),
        _types("Component", "Code", "Requirement", "Control"),
        ("impacts", "concerns", "is about", "breaks", "is reported against"),
    ),
    "violates": Relation(
        _types("Ticket", "Incident", "Component", "Code"),
        _types("Requirement", "Control"),
        ("breaches", "does not meet", "fails", "is not compliant with", "does not satisfy"),
    ),
    "required by": Relation(
        _types("Requirement", "Control"),
        _types("Requirement", "Control"),
        ("is required by", "mandated by", "is mandated by", "derives from", "traces to"),
    ),
    "implemented in": Relation(
        _types("Requirement", "Control", "Component", "Code"),
        _types("Code", "Component"),
        ("is implemented in", "implemented by", "is implemented by", "is defined in", "lives in"),
    ),
    "covers": Relation(
        _types("Runbook"),
        _types("Component", "Incident", "Change", "Requirement", "Control", "Code"),
        ("documents", "describes", "applies to", "explains"),
    ),
    "is on": Relation(
        _types("Incident"),
        _types("Component"),
        ("occurred on", "occurred in", "happened on", "hit", "is in"),
    ),
    "modifies": Relation(
        _types("Change", "Ticket"),
        _types("Component", "Code"),
        ("changes", "updates", "deploys", "releases", "alters"),
    ),
    "fixes": Relation(
        _types("Change", "Ticket", "Code"),
        _types("Ticket", "Incident"),
        ("resolves", "closes", "addresses", "remediates", "fixed"),
    ),
    "deployed to": Relation(
        _types("Component", "Change"),
        _types("Component"),
        ("is deployed to", "runs on", "is hosted on", "hosted on"),
    ),
    "verifies": Relation(
        _types("Code", "Ticket", "Runbook"),
        _types("Requirement", "Control", "Component", "Code"),
        ("tests", "validates", "checks", "is tested by"),
    ),
    "references": Relation(ANY, ANY, ("mentions", "refers to", "cites", "links to")),
}


def _phrase(text):
    return " ".join(text.casefold().replace("_", " ").split())


_RELATION_NAMES = {
    _phrase(alias): name
    for name, relation in RELATIONS.items()
    for alias in (name, *relation.aliases)
}
_TYPE_NAMES = {_phrase(name): name for name in TYPES} | {
    _phrase(alias): name for alias, name in TYPE_ALIASES.items()
}


def entity_type(text):
    """The canonical type for a model's type name, or None if it is not one."""
    return _TYPE_NAMES.get(_phrase(text)) if isinstance(text, str) else None


def relation(text):
    """The canonical relation for a model's phrasing, or None if it is not one."""
    return _RELATION_NAMES.get(_phrase(text)) if isinstance(text, str) else None


def typed(subject_type, name, object_type):
    """(subject type, relation, object type), canonical, or None if not allowed.

    None when any part is unknown, or the relation does not join those types:
    "Requirement violates Ticket" is the wrong way round and is refused rather
    than silently reversed.
    """
    subject, rel, obj = entity_type(subject_type), relation(name), entity_type(object_type)
    if not (subject and rel and obj):
        return None
    allowed = RELATIONS[rel]
    if subject not in allowed.subjects or obj not in allowed.objects:
        return None
    return subject, rel, obj


def prompt():
    """The ontology as the extraction prompt states it, built from the registry."""
    types = "; ".join(f"{name} ({meaning})" for name, meaning in TYPES.items())
    relations = "; ".join(
        f"'{name}' ({'/'.join(sorted(rule.subjects)) if rule.subjects != ANY else 'any'} -> "
        f"{'/'.join(sorted(rule.objects)) if rule.objects != ANY else 'any'})"
        for name, rule in RELATIONS.items()
    )
    return (
        f"Every entity has a type, exactly one of: {types}. "
        f"Every relation is exactly one of these, from subject type to object type: "
        f"{relations}. Use 'references' only when no other relation fits. "
        "Skip entities of any other type, such as people or teams."
    )


# ── merging names ────────────────────────────────────────────────────────────

#: Two words this alike (Jaro-Winkler) are one word misspelt - if both are
#: long enough for that to mean anything.
SIMILARITY = 0.93
#: Shorter words must match exactly (after stemming): "blue" and "green" name
#: two deployments, and "api" and "apis" are close as strings only.
MIN_FUZZY_LENGTH = 6


def normalise(label):
    """A name with case, accents, punctuation and separators taken out.

    "CarePath-API_green", "carepath api green" and "CarePath API Green." all
    become "carepath api green".
    """
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w]+|_", " ", text.casefold())
    text = " ".join(text.split())
    return text.removeprefix("the ")


def _alike(left, right):
    """Whether two normalised names are one thing, word by word.

    The same number of words, each the same after stemming or a long word
    misspelt. Comparing whole strings was tried and merged carepath-api with
    carepath-api-green: a shared prefix scores high, and those are two
    deployments - CARE-444 is exactly that mistake. Numbers must match exactly,
    so CARE-401 and CARE-412 stay two tickets.
    """
    from rapidfuzz.distance import JaroWinkler

    from .ranking import stem

    if left == right:
        return True
    words, others = left.split(), right.split()
    if len(words) != len(others):
        return False
    for word, other in zip(words, others, strict=True):
        if word == other or stem(word) == stem(other):
            continue
        if (
            min(len(word), len(other)) < MIN_FUZZY_LENGTH
            or any(character.isdigit() for character in word + other)
            or JaroWinkler.similarity(word, other) < SIMILARITY
        ):
            return False
    return True


def merge_entities(app_id, data):
    """One node per typed AI entity, however many sources named it.

    Extraction keys an entity to its source, because an identical label is not
    proof of sameness. Here two entities become one only when they have the
    same type and their names normalise identically or are near-identical with
    the same numbers - so a Component never merges with a Requirement, and
    CARE-401 never merges with CARE-412. Untyped entities (from versions
    extracted before the ontology) are left alone.

    Each edge keeps its own source, digest, line and quote, so citations are
    verified edge by edge exactly as before; the merged node only records the
    names it was known by. Returns how many nodes were folded away.
    """
    nodes = data["nodes"]
    entities = [n for n in nodes if n.get("kind") == "entity" and n.get("type") in TYPES]
    groups = []  # [(type, representative key, [nodes])]
    for node in sorted(entities, key=lambda n: (n["type"], normalise(n["label"]), n["id"])):
        key = normalise(node["label"])
        if not key:
            continue
        home = next(
            (g for g in groups if g[0] == node["type"] and _alike(g[1], key)),
            None,
        )
        if home is None:
            groups.append((node["type"], key, [node]))
        else:
            home[2].append(node)
    remap = {}
    merged = {}
    for kind, key, members in groups:
        if len(members) < 2:
            continue
        identity = hashlib.sha256(f"{app_id}:entity:{kind}:{key}".encode()).hexdigest()[:24]
        # A member may itself be a node merged by an earlier version and carried
        # forward by an incremental run: keep the names and sources it had.
        labels = sorted(
            {m["label"] for m in members}
            | {alias for m in members for alias in m.get("aliases", [])}
        )
        # The name most sources used, then the shortest: a stable choice.
        label = min(
            {m["label"] for m in members},
            key=lambda text: (-sum(m["label"] == text for m in members), len(text), text),
        )
        sources = sorted(
            {m["knowledge_id"] for m in members if m.get("knowledge_id")}
            | {source for m in members for source in m.get("knowledge_ids", [])}
        )
        survivor = {
            "id": identity,
            "kind": "entity",
            "type": kind,
            "label": label,
            "aliases": labels,
            "knowledge_ids": sources,
        }
        if len(sources) == 1:
            # Still one document's entity: keep the link to open it.
            survivor["knowledge_id"] = sources[0]
            survivor["line"] = min(m.get("line") or 0 for m in members) or None
        merged[identity] = survivor
        for member in members:
            remap[member["id"]] = identity
    if not remap:
        return 0
    kept = [n for n in nodes if n["id"] not in remap]
    data["nodes"] = kept + list(merged.values())
    edges, seen = [], set()
    for edge in data["edges"]:
        edge = dict(
            edge,
            source=remap.get(edge["source"], edge["source"]),
            target=remap.get(edge["target"], edge["target"]),
        )
        if edge["source"] == edge["target"]:
            # "carepath-api-green is deployed to CarePath API green" said nothing.
            continue
        signature = (edge["source"], edge["target"], edge["relation"], edge.get("knowledge_id"))
        if edge.get("relation") == "mentions entity" and signature in seen:
            continue
        seen.add(signature)
        edges.append(edge)
    data["edges"] = edges
    return len(remap) - len(merged)


def aliases_for(data, names):
    """More names for operations components, from the knowledge graph's merges.

    `names` maps a case-folded component name to its operations node key. A
    merged Component entity that answers to one of them contributes its other
    names, so a runbook that writes "CarePath API green" reaches
    carepath-api-green. Only names that normalise to the component itself are
    joined - never a shorter or longer one.
    """
    wanted = {normalise(name): key for name, key in names.items()}
    found = {}
    for node in data.get("nodes", []):
        if node.get("type") != "Component" or not node.get("aliases"):
            continue
        keys = {wanted[normalise(alias)] for alias in node["aliases"] if normalise(alias) in wanted}
        if len(keys) != 1:
            # Answers to no component, or to two: joining would be a guess.
            continue
        key = keys.pop()
        for alias in node["aliases"]:
            found.setdefault(alias.casefold(), key)
    return {name: key for name, key in found.items() if name not in names}
