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

**An application may add one industry domain** (`PACKS`, `Application.ontology`,
chosen on the create form and on Generate graph). A pack is this registry plus
its own types and relations - never instead of it, because Code Factory,
ServiceOps and code review read Ticket, Component and Code whatever the
industry. Blank is this registry alone, exactly as before: same prompt, same
cache rubric, same fingerprint. A domain's own name wins over a core alias for
something else ("policy" is a Policy in insurance, not a Control), and the same
phrasing may name two relations so long as their types never overlap - `typed`
takes the one whose types fit. Two rules hold for a pack's own relations only:
a quote that negates (`NEGATION`) supports none of them unless the relation
itself says something is absent (`Relation.negative`: an exclusion, a
contraindication), and a person type (`Ontology.masked_types`) is accepted only
under a masking placeholder.
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
    #: The relation itself says something is absent - an exclusion, a
    #: contraindication - so a negated quote is what supports it.
    negative: bool = False


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


#: A quote that says something is not so: "does not cover", "no evidence of",
#: "ruled out". "No." and "No:" are a number's label ("Policy No. 4512"), not a
#: denial. Deliberately broad - refusing a true relationship costs one edge, and
#: accepting "covers flood" from "does not cover flood" costs the graph its word.
NEGATION = re.compile(
    r"\b(?:not|never|none|neither|nor|without|cannot|denies|denied|absent|excluding|except"
    r"|ruled out|negative for)\b|\bno\b(?![.:#])|n't\b",
    re.I,
)


#: A person's name as masking writes it (`masking.placeholder` under the
#: health labels): `[MRN-QZKTWBAF]`. A test holds it to `masking.LABELS`.
MASKED = re.compile(r"\[(?:MRN|MEMBER-ID|NHS-NUMBER|PATIENT)-[A-Z]{8}\]")


def _spoken(names):
    words = [name.lower() for name in names]
    return ", ".join(words[:-1]) + f" or {words[-1]}" if len(words) > 1 else "".join(words)


@dataclass(frozen=True, eq=False)
class Ontology:
    """One closed registry: types, the relations between them, and how a prompt says so."""

    #: `Application.ontology`; blank is the core registry alone.
    key: str
    label: str
    description: str
    types: dict
    type_aliases: dict
    relations: dict
    #: What a source can itself be, so may be the subject of what it says.
    self_types: tuple
    #: The prompt's sentence on which relationships matter most.
    preference: str
    #: The prompt's last sentence, on entities of no type here.
    others: str
    #: Relations a pack added, held to the negation rule.
    domain_relations: frozenset = frozenset()
    #: Types accepted only under a masking placeholder (`MASKED`).
    masked_types: frozenset = frozenset()
    #: Bump when one of the pack's own types or relations changes meaning.
    version: int = 1

    def __post_init__(self):
        everything = frozenset(self.types)
        relation_names = {}
        # Canonical names first, then phrasings: "covers" is the relation of
        # that name before it is anything else's alias.
        for phrases in (
            [(name, name) for name in self.relations],
            [(alias, name) for name, rule in self.relations.items() for alias in rule.aliases],
        ):
            for phrase, name in phrases:
                found = relation_names.setdefault(_phrase(phrase), [])
                if name not in found:
                    found.append(name)
        type_names = {_phrase(name): name for name in self.types} | {
            _phrase(alias): name for alias, name in self.type_aliases.items()
        }
        object.__setattr__(self, "any", everything)
        object.__setattr__(
            self, "_relation_names", {k: tuple(v) for k, v in relation_names.items()}
        )
        object.__setattr__(self, "_type_names", type_names)

    def entity_type(self, text):
        """The canonical type for a model's type name, or None if it is not one."""
        return self._type_names.get(_phrase(text)) if isinstance(text, str) else None

    def relation(self, text):
        """The canonical relation a phrasing names first, or None if it names none."""
        names = self._relation_names.get(_phrase(text), ()) if isinstance(text, str) else ()
        return names[0] if names else None

    def typed(self, subject_type, name, object_type):
        """(subject type, relation, object type), canonical, or None if not allowed.

        None when any part is unknown, or the relation does not join those types:
        "Requirement violates Ticket" is the wrong way round and is refused rather
        than silently reversed. A phrasing two relations share is the one whose
        types fit - `check` holds them to never both fitting.
        """
        subject, obj = self.entity_type(subject_type), self.entity_type(object_type)
        names = self._relation_names.get(_phrase(name), ()) if isinstance(name, str) else ()
        if not (subject and obj and names):
            return None
        for rel in names:
            allowed = self.relations[rel]
            if subject in allowed.subjects and obj in allowed.objects:
                return subject, rel, obj
        return None

    def prompt(self):
        """The ontology as the extraction prompt states it, built from the registry."""
        types = "; ".join(f"{name} ({meaning})" for name, meaning in self.types.items())
        relations = "; ".join(
            f"'{name}' "
            f"({'/'.join(sorted(rule.subjects)) if rule.subjects != self.any else 'any'} -> "
            f"{'/'.join(sorted(rule.objects)) if rule.objects != self.any else 'any'})"
            for name, rule in self.relations.items()
        )
        return (
            f"Every entity has a type, exactly one of: {types}. "
            f"Every relation is exactly one of these, from subject type to object type: "
            f"{relations}. Use 'references' only when no other relation fits. "
            + self.others
        )

    def self_phrase(self):
        """"ticket, runbook, incident or change", for the extraction prompt."""
        return _spoken(self.self_types)

    def refusal(self, relation_name, quote, subject, subject_type, obj, object_type):
        """Why a relationship this ontology typed is still refused, or None.

        Only ever for a pack's own relations and types, so the core registry
        refuses exactly what it always did.
        """
        rule = self.relations[relation_name]
        if relation_name in self.domain_relations and not rule.negative and NEGATION.search(quote):
            return "negated quote"
        for name, kind in ((subject, subject_type), (obj, object_type)):
            if kind in self.masked_types and not MASKED.fullmatch(name.strip()):
                return "unmasked person"
        return None

    def check(self):
        """Faults in the registry itself; empty when it is sound. Pinned by a test."""
        faults = []
        for name, rule in self.relations.items():
            for end in (rule.subjects, rule.objects):
                faults += [f"{name}: no type {kind}" for kind in sorted(end - self.any)]
        for phrase, names in self._relation_names.items():
            pairs = [
                {(s, o) for s in self.relations[n].subjects for o in self.relations[n].objects}
                for n in names
                if n != "references"
            ]
            for index, left in enumerate(pairs):
                for right in pairs[index + 1 :]:
                    if left & right:
                        faults.append(f"'{phrase}' names two relations between the same types")
        faults += [
            f"alias '{alias}': no type {kind}"
            for alias, kind in self.type_aliases.items()
            if kind not in self.types
        ]
        faults += [f"self type {t} is not a type" for t in self.self_types if t not in self.types]
        faults += [f"masked type {t} is not a type" for t in self.masked_types - self.any]
        return faults


#: What a source can itself be, in the core registry.
CORE_SELF_TYPES = ("Ticket", "Runbook", "Incident", "Change")

CORE = Ontology(
    key="",
    label="Software delivery and operations",
    description="Tickets, requirements, controls, components, code, runbooks, incidents "
    "and changes.",
    types=TYPES,
    type_aliases=TYPE_ALIASES,
    relations=RELATIONS,
    self_types=CORE_SELF_TYPES,
    preference="Prefer relationships a reader would care about: what a ticket affects or "
    "violates, what code implements a requirement, what a component depends on, what a "
    "runbook covers.",
    others="Skip entities of any other type, such as people or teams.",
)


def entity_type(text):
    """The core registry's type for a model's type name, or None if it is not one."""
    return CORE.entity_type(text)


def relation(text):
    """The core registry's relation for a model's phrasing, or None if it is not one."""
    return CORE.relation(text)


def typed(subject_type, name, object_type):
    """`Ontology.typed` under the core registry."""
    return CORE.typed(subject_type, name, object_type)


def prompt():
    """The core registry as the extraction prompt states it."""
    return CORE.prompt()


# ── industry domains ─────────────────────────────────────────────────────────

#: Phrasings `subject to` absorbs, in both packs.
_SUBJECT_TO = (
    "must comply with",
    "regulated by",
    "is regulated by",
    "governed by",
    "is subject to",
)


def _pack(key, label, description, *, types, aliases, relations, self_types, preference, others,
          overrides=None, masked=()):
    """The core registry plus one domain's own types and relations."""
    claimed = {_phrase(name) for name in types} | {_phrase(alias) for alias in aliases}
    everything = frozenset(TYPES) | frozenset(types)
    return Ontology(
        key=key,
        label=label,
        description=description,
        types={**TYPES, **(overrides or {}), **types},
        # A domain's own word wins: "policy" is a Policy here, not a Control.
        type_aliases={a: t for a, t in TYPE_ALIASES.items() if _phrase(a) not in claimed}
        | aliases,
        relations={name: rule for name, rule in RELATIONS.items() if name != "references"}
        | relations
        | {"references": Relation(everything, everything, RELATIONS["references"].aliases)},
        self_types=CORE_SELF_TYPES + tuple(self_types),
        preference=preference,
        others=others,
        domain_relations=frozenset(relations),
        masked_types=frozenset(masked),
    )


US_INSURANCE = _pack(
    "us_insurance",
    "US insurance",
    "Property and casualty, life and annuity: carriers, producers, policies, forms, "
    "coverages, claims, perils, and state regulation and filings.",
    types={
        "Carrier": "an insurer, reinsurer, risk retention group or surplus lines insurer",
        "Producer": "an agent, broker, MGA, wholesaler or program administrator",
        "Party": "a named or additional insured, policyholder, claimant or beneficiary",
        "Treaty": "a reinsurance treaty or facultative certificate",
        "Product": "a line of business or insurance program",
        "Policy": "an issued insurance contract, by its policy number",
        "Form": "a numbered policy form or endorsement, such as ISO HO 00 03",
        "Coverage": "a coverage part, insuring agreement, limit or deductible",
        "Exclusion": "a clause that removes cover",
        "Guideline": "an underwriting, rating or claims-handling rule",
        "Claim": "a claim or claim feature, by its claim number",
        "Peril": "a cause of loss, such as wind, hail, fire or theft",
        "Loss event": "a single occurrence, including a named catastrophe",
        "Risk": "what is insured: a location, vehicle, operation or insured life",
        "Jurisdiction": "a US state or territory whose insurance law applies",
        "Regulator": "a body that supervises insurance, such as a state Department of Insurance",
        "Filing": "a rate, rule or form filing with a state, such as a SERFF filing",
    },
    overrides={"Control": "a regulation, statute, model law, bulletin or standard clause"},
    aliases={
        "insurer": "Carrier",
        "insurance company": "Carrier",
        "writing company": "Carrier",
        "reinsurer": "Carrier",
        "risk retention group": "Carrier",
        "surplus lines insurer": "Carrier",
        "agent": "Producer",
        "broker": "Producer",
        "mga": "Producer",
        "managing general agent": "Producer",
        "wholesaler": "Producer",
        "agency": "Producer",
        "insured": "Party",
        "named insured": "Party",
        "additional insured": "Party",
        "policyholder": "Party",
        "claimant": "Party",
        "beneficiary": "Party",
        "reinsurance treaty": "Treaty",
        "quota share": "Treaty",
        "excess of loss": "Treaty",
        "facultative certificate": "Treaty",
        "line of business": "Product",
        "lob": "Product",
        "program": "Product",
        "insurance product": "Product",
        "insurance policy": "Policy",
        "binder": "Policy",
        "declarations": "Policy",
        "dec page": "Policy",
        "endorsement": "Form",
        "rider": "Form",
        "policy form": "Form",
        "coverage form": "Form",
        "coverage part": "Coverage",
        "insuring agreement": "Coverage",
        "limit": "Coverage",
        "deductible": "Coverage",
        "benefit": "Coverage",
        "exclusion clause": "Exclusion",
        "limitation": "Exclusion",
        "underwriting guideline": "Guideline",
        "underwriting rule": "Guideline",
        "rating rule": "Guideline",
        "rate manual": "Guideline",
        "claims guideline": "Guideline",
        "appetite": "Guideline",
        "fnol": "Claim",
        "first notice of loss": "Claim",
        "notice of loss": "Claim",
        "claim feature": "Claim",
        "cause of loss": "Peril",
        "covered peril": "Peril",
        "hazard": "Peril",
        "lossevent": "Loss event",
        "occurrence": "Loss event",
        "catastrophe": "Loss event",
        "cat event": "Loss event",
        "exposure": "Risk",
        "insured property": "Risk",
        "insured location": "Risk",
        "insured vehicle": "Risk",
        "insured life": "Risk",
        "state": "Jurisdiction",
        "territory": "Jurisdiction",
        "department of insurance": "Regulator",
        "doi": "Regulator",
        "insurance department": "Regulator",
        "insurance commissioner": "Regulator",
        "rate filing": "Filing",
        "form filing": "Filing",
        "rule filing": "Filing",
        "serff filing": "Filing",
    },
    relations={
        "issued by": Relation(
            _types("Policy"),
            _types("Carrier"),
            ("is issued by", "written by", "underwritten by", "on the paper of"),
        ),
        "held by": Relation(
            _types("Policy"),
            _types("Party"),
            ("is held by", "owned by", "in the name of", "issued to"),
        ),
        "placed by": Relation(
            _types("Policy"),
            _types("Producer"),
            ("written through", "brokered by", "sold by", "produced by"),
        ),
        "written on": Relation(
            _types("Policy"),
            _types("Form", "Product"),
            ("is written on", "uses form", "attaches", "issued on"),
        ),
        "includes": Relation(
            _types("Form", "Policy", "Product"),
            _types("Coverage", "Exclusion"),
            ("contains", "provides", "has"),
        ),
        "amends": Relation(
            _types("Form"),
            _types("Form", "Coverage", "Exclusion"),
            ("endorses", "modifies", "adds", "deletes", "replaces"),
        ),
        "insures": Relation(
            _types("Policy", "Coverage"),
            _types("Risk", "Party"),
            ("covers the", "schedules", "protects"),
        ),
        "covers peril": Relation(
            _types("Coverage"),
            _types("Peril"),
            ("covers", "protects against", "pays for loss from", "responds to", "insures against"),
        ),
        "excludes": Relation(
            _types("Exclusion", "Form", "Policy"),
            _types("Peril", "Risk", "Coverage"),
            ("does not cover", "does not apply to", "we will not pay for", "carves out",
             "not covered"),
            negative=True,
        ),
        "governs": Relation(
            _types("Guideline"),
            _types("Product", "Coverage", "Risk", "Claim"),
            ("applies to", "sets eligibility for", "rates", "determines"),
        ),
        "made under": Relation(
            _types("Claim"),
            _types("Policy", "Coverage"),
            ("is made under", "filed against", "claimed under", "submitted under",
             "tendered under"),
        ),
        "caused by": Relation(
            _types("Claim", "Loss event"),
            _types("Peril"),
            ("is caused by", "due to", "resulting from", "arising out of"),
        ),
        "arises from": Relation(
            _types("Claim"),
            _types("Loss event"),
            ("arising from", "part of", "from the occurrence"),
        ),
        "concerns": Relation(
            _types("Claim"),
            _types("Risk", "Party"),
            ("for damage to", "involves", "relates to"),
        ),
        "licensed in": Relation(
            _types("Carrier", "Producer"),
            _types("Jurisdiction"),
            ("is licensed in", "admitted in", "authorized in", "appointed in"),
        ),
        "domiciled in": Relation(
            _types("Carrier"),
            _types("Jurisdiction"),
            ("is domiciled in", "incorporated in"),
        ),
        "ceded under": Relation(
            _types("Policy", "Product"),
            _types("Treaty"),
            ("is ceded under", "reinsured under", "ceded to"),
        ),
        "reinsured by": Relation(
            _types("Treaty"),
            _types("Carrier"),
            ("is reinsured by", "placed with", "led by"),
        ),
        "files": Relation(
            _types("Filing"),
            _types("Form", "Product", "Guideline"),
            ("approves", "is filed for", "filed for"),
        ),
        "filed with": Relation(
            _types("Filing"),
            _types("Regulator", "Jurisdiction"),
            ("is filed with", "submitted to", "approved by"),
        ),
        "enforces": Relation(
            _types("Regulator"),
            _types("Control"),
            ("issued", "issues", "adopted", "administers"),
        ),
        "applies in": Relation(
            _types("Control"),
            _types("Jurisdiction"),
            ("in force in", "adopted by", "enacted in"),
        ),
        "subject to": Relation(
            _types("Form", "Filing", "Guideline", "Claim", "Policy", "Product", "Carrier",
                   "Producer"),
            _types("Control"),
            _SUBJECT_TO,
        ),
    },
    self_types=("Policy", "Claim", "Filing", "Form", "Guideline"),
    preference="Prefer relationships a reader would care about: what a policy is written on "
    "and includes, what a coverage covers or excludes, what a claim is made under and caused "
    "by, which state a filing or carrier is in, and which regulation applies.",
    others="Skip entities of any other type, such as employees or teams. Name a party exactly "
    "as the text does; where the text holds a masked placeholder in square brackets, use it.",
)


HEALTHCARE = _pack(
    "healthcare",
    "Healthcare and life sciences",
    "Patients (masked), conditions, medications, procedures, observations, encounters, "
    "providers, guidelines, trials, products, adverse events and biomarkers.",
    types={
        "Patient": "a patient, named only by the masked placeholder the text uses, "
        "such as [MRN-QZKTWBAF]",
        "Condition": "a disease, diagnosis, disorder or clinical finding",
        "Medication": "a drug, substance, formulation or dose regimen",
        "Procedure": "a surgery, intervention, therapy or test performed on a patient",
        "Observation": "a lab test, vital sign, score or measurement",
        "Encounter": "a visit, admission or episode of care",
        "Provider": "a clinician role, care team, facility or healthcare organisation",
        "Guideline": "a clinical guideline, care pathway, protocol or order set",
        "Trial": "a clinical study or one of its arms, such as NCT01234567",
        "Product": "a medical device, biologic or other regulated product",
        "Adverse event": "a side effect, adverse reaction or safety signal",
        "Biomarker": "a gene, variant or biomarker",
    },
    overrides={"Runbook": "a runbook, SOP or knowledge article"},
    aliases={
        "subject": "Patient",
        "participant": "Patient",
        "diagnosis": "Condition",
        "disease": "Condition",
        "disorder": "Condition",
        "finding": "Condition",
        "drug": "Medication",
        "medicine": "Medication",
        "substance": "Medication",
        "surgery": "Procedure",
        "intervention": "Procedure",
        "operation": "Procedure",
        "clinical procedure": "Procedure",
        "lab": "Observation",
        "lab test": "Observation",
        "laboratory test": "Observation",
        "test result": "Observation",
        "vital sign": "Observation",
        "measurement": "Observation",
        "visit": "Encounter",
        "admission": "Encounter",
        "appointment": "Encounter",
        "episode of care": "Encounter",
        "clinician": "Provider",
        "physician": "Provider",
        "care team": "Provider",
        "hospital": "Provider",
        "facility": "Provider",
        "clinic": "Provider",
        "clinical guideline": "Guideline",
        "pathway": "Guideline",
        "care pathway": "Guideline",
        "protocol": "Guideline",
        "order set": "Guideline",
        "study": "Trial",
        "clinical trial": "Trial",
        "clinical study": "Trial",
        "trial arm": "Trial",
        "device": "Product",
        "medical device": "Product",
        "implant": "Product",
        "biologic": "Product",
        "adverseevent": "Adverse event",
        "side effect": "Adverse event",
        "adverse reaction": "Adverse event",
        "adverse drug reaction": "Adverse event",
        "adr": "Adverse event",
        "safety signal": "Adverse event",
        "gene": "Biomarker",
        "variant": "Biomarker",
        "mutation": "Biomarker",
        "marker": "Biomarker",
        "sop": "Runbook",
        "standard operating procedure": "Runbook",
    },
    relations={
        "treats": Relation(
            _types("Medication", "Procedure", "Product"),
            _types("Condition"),
            ("is used to treat", "manages", "is a treatment for"),
        ),
        "indicated for": Relation(
            _types("Medication", "Product", "Procedure"),
            _types("Condition"),
            ("is indicated for", "is indicated in", "is approved for", "licensed for"),
        ),
        "contraindicated with": Relation(
            _types("Medication", "Procedure"),
            _types("Condition", "Medication"),
            ("is contraindicated with", "is contraindicated in", "contraindicated in",
             "must not be used with", "should not be used in"),
            negative=True,
        ),
        "interacts with": Relation(
            _types("Medication"),
            _types("Medication"),
            ("has an interaction with", "potentiates", "inhibits"),
        ),
        "causes": Relation(
            _types("Medication", "Product", "Procedure"),
            _types("Adverse event"),
            ("may cause", "can cause", "is associated with", "can lead to", "leads to"),
        ),
        "diagnosed with": Relation(
            _types("Patient"),
            _types("Condition"),
            ("was diagnosed with", "is diagnosed with", "has", "presents with",
             "has a history of"),
        ),
        "receives": Relation(
            _types("Patient"),
            _types("Medication", "Procedure"),
            ("was prescribed", "is prescribed", "was administered", "underwent", "is on",
             "takes"),
        ),
        "measured by": Relation(
            _types("Condition", "Medication"),
            _types("Observation"),
            ("is measured by", "is monitored by", "is assessed by", "is tracked with"),
        ),
        "recommends": Relation(
            _types("Guideline"),
            _types("Medication", "Procedure", "Observation"),
            ("advises", "calls for"),
        ),
        "attended": Relation(
            _types("Patient"),
            _types("Encounter"),
            ("was admitted", "was seen at", "visited"),
        ),
        "performed by": Relation(
            _types("Encounter", "Procedure"),
            _types("Provider"),
            ("is performed by", "was performed by", "was carried out by", "attended by"),
        ),
        "studies": Relation(
            _types("Trial"),
            _types("Medication", "Product", "Condition", "Biomarker"),
            ("investigates", "evaluates", "compares"),
        ),
        "enrolled in": Relation(
            _types("Patient"),
            _types("Trial"),
            ("is enrolled in", "was randomised to", "was randomized to", "participates in"),
        ),
        "biomarker for": Relation(
            _types("Biomarker"),
            _types("Condition", "Medication"),
            ("is a biomarker for", "is a marker of", "predicts response to"),
        ),
        "subject to": Relation(
            _types("Medication", "Product", "Procedure", "Trial", "Guideline"),
            _types("Control"),
            _SUBJECT_TO,
        ),
    },
    self_types=("Guideline", "Trial"),
    preference="Prefer relationships a reader would care about: what treats or is "
    "contraindicated with a condition, what a guideline recommends, what a medication causes "
    "or interacts with, and what a trial studies.",
    others="Skip entities of any other type, such as named staff or teams. Name a patient "
    "only by the masked placeholder the text uses; skip a patient named any other way.",
    masked=("Patient",),
)

#: Every ontology an application may follow, by `Application.ontology`.
PACKS = {pack.key: pack for pack in (CORE, US_INSURANCE, HEALTHCARE)}
CHOICES = [(pack.key, pack.label) for pack in PACKS.values()]


def for_application(app_id):
    """The ontology an application follows: its pack, else the core registry."""
    from .models import Application

    key = Application.objects.filter(pk=app_id).values_list("ontology", flat=True).first()
    return PACKS.get(key or "", CORE)


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


def merge_entities(app_id, data, types=None):
    """One node per typed AI entity, however many sources named it.

    Extraction keys an entity to its source, because an identical label is not
    proof of sameness. Here two entities become one only when they have the
    same type and their names normalise identically or are near-identical with
    the same numbers - so a Component never merges with a Requirement, and
    CARE-401 never merges with CARE-412. Untyped entities (from versions
    extracted before the ontology) are left alone.

    Each edge keeps its own source, digest, line and quote, so citations are
    verified edge by edge exactly as before; the merged node only records the
    names it was known by. Returns how many nodes were folded away. `types` is
    the ontology's, the core registry's when not given.
    """
    nodes = data["nodes"]
    known = TYPES if types is None else types
    entities = [n for n in nodes if n.get("kind") == "entity" and n.get("type") in known]
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
