"""How well the AI typing fallback types what the rules cannot, measured on a frozen pack.

    manage.py typing_pack --build-pack
    manage.py typing_pack <application id> --as <username> [--record]

`benchmarks/typing-pack.json` holds only documents the rules leave as Other
with low confidence - the one case `document_ai.classify` is asked about - each
with the type a person would give it. They come from two places: every CarePath
document with its title line and file name removed (how `scorecard` measures
reading content rather than names) that the rules then miss, and a few written
here that no rule could type. Labels are a person's, from
`benchmarks/document-types.json` and below, never what a model said; Other is a
label too, and a model that names a type for one of those is a false positive.

A run sends each item through exactly what production sends - the same
instructions, the same `judge_type` - with no file name, through the
application's own `document_typing` configuration, credential and budget, one
receipted call an item. Like `llm_drift`, it is run deliberately and never by
the test suite. Rebuilding the pack starts a new reference: do it only when
the corpus or the registry changes.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from . import document_ai, document_types

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "benchmarks" / "typing-pack.json"
RECORDS = ROOT / "benchmarks" / "typing"

#: Short documents a person would type at a glance and the rules cannot: their
#: kind is in what they say, not in a title, heading or column a rule names.
WRITTEN = [
    {
        "id": "written/weekly-sync",
        "label": "review",
        "text": (
            "Present: Amara (clinical lead), Jonas (engineering), Wei (records team)\n"
            "Apologies: Dr Patel\n\n"
            "1. Consent export defect. Jonas said the fix is ready for review. Agreed it ships "
            "only after Wei signs off the audit wording.\n"
            "2. Pilot ward rollout. Amara asked to move the start to 3 March. Agreed.\n"
            "3. Open risks. Nothing new raised.\n\n"
            "Decided: the release waits for the consent fix. Jonas to circulate the test "
            "evidence by Friday; Wei to confirm the audit wording by Monday.\n"
        ),
    },
    {
        "id": "written/outage-summary",
        "label": "postmortem",
        "text": (
            "On Tuesday the referral service stopped accepting new referrals for 47 minutes.\n\n"
            "09:12 the nightly certificate rotation replaced the database client certificate.\n"
            "09:14 the first referral submissions failed with a TLS handshake error.\n"
            "09:31 the on-call engineer was paged by the error-rate alert.\n"
            "09:59 the previous certificate was restored and submissions recovered.\n\n"
            "The rotation job had never been run against the referral database, because it "
            "was added to the rotation list after the last test. 212 referrals were rejected "
            "and resubmitted by ward staff by hand.\n\n"
            "We will add the referral database to the rotation test (owner Jonas, by 14 March) "
            "and alert on certificate expiry 14 days ahead (owner Wei, by 21 March).\n"
        ),
    },
    {
        "id": "written/what-changed",
        "label": "release_notes",
        "text": (
            "CarePath 2.4 is available from today.\n\n"
            "Clinicians can now see where a referral is once it leaves their ward, and the "
            "referral list can be filtered by receiving clinic.\n"
            "The patient search no longer times out for surnames shared by more than 500 "
            "patients.\n"
            "Exports to partner organisations now check the patient's consent first, and an "
            "export without consent is refused and recorded.\n\n"
            "Nothing needs to be done to upgrade: the change was applied overnight. If a "
            "referral looks wrong, call the service desk on extension 4400.\n"
        ),
    },
    {
        "id": "written/what-could-go-wrong",
        "label": "risk_register",
        "text": (
            "Things that could stop the pilot, as of February:\n\n"
            "- The community clinics may not have scanners by March. Fairly likely; it would "
            "delay the pilot a month. Amara is chasing the order.\n"
            "- Medical record numbers might not be unique across the network. Unlikely, but "
            "referrals would be filed against the wrong patient. Wei is checking a sample.\n"
            "- The partner export could ship before consent is checked. We have seen it "
            "happen once; it would be a reportable breach. Jonas owns the fix, now in test.\n"
        ),
    },
    {
        "id": "written/lunch-rota",
        "label": "other",
        "text": (
            "Tea and coffee: please wash your own mug.\n\n"
            "Monday - Amara brings milk\nTuesday - Jonas\nWednesday - Wei\n"
            "Thursday - whoever forgot last week\nFriday - cake, if anyone is feeling generous\n\n"
            "The fridge is cleared every Friday at four. Anything without a name goes.\n"
        ),
    },
    {
        "id": "written/letter",
        "label": "other",
        "text": (
            "Dear Ms Okafor,\n\n"
            "Thank you for coming to the clinic on 12 February. As we discussed, your "
            "next appointment is on 3 April at 10:30. Please bring a list of the medicines "
            "you are taking.\n\n"
            "If you need to change the appointment, call the clinic on the number at the top "
            "of this letter.\n\nYours sincerely,\nCommunity respiratory team\n"
        ),
    },
]


def untitled(text):
    """The text without its title line, as `scorecard.document_typing` reads it."""
    title_line = document_types.parse(text).title_line
    lines = text.splitlines()
    return "\n".join(lines[: title_line - 1] + lines[title_line:]) if title_line else text


def build_pack():
    """The frozen items, from the repository. No model and no database."""
    from .scorecard import BENCH, DOCS, convert

    labels = json.loads((BENCH / "document-types.json").read_text(encoding="utf-8"))
    labels = labels["documents"]
    items = []
    for path in sorted(p for p in DOCS.rglob("*") if p.is_file()):
        text = convert(path)
        name = path.relative_to(DOCS).as_posix()
        if text is None or name not in labels:
            continue
        bare = untitled(text)
        if document_ai.low_confidence(document_types.classify(bare)):
            items.append({"id": f"carepath/{name}", "label": labels[name],
                          "text": bare[: document_ai.MAX_CHARACTERS]})
    for item in WRITTEN:
        if not document_ai.low_confidence(document_types.classify(item["text"])):
            raise ValueError(f"{item['id']} is typed by the rules; it does not belong here.")
        items.append(item)
    pack = {
        "about": "Documents the rules leave as Other, with the type a person would give each. "
                 "Built by `manage.py typing_pack --build-pack`; see platform_core/typing_pack.py.",
        "registry_version": document_types.REGISTRY_VERSION,
        "items": items,
    }
    PACK.write_text(json.dumps(pack, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return pack


def load_pack():
    return json.loads(PACK.read_text(encoding="utf-8"))


def score(results):
    """The measures, from [(label, verdict)] - what is accepted is what production uses."""
    typed = [(label, v) for label, v in results if label != document_types.OTHER]
    others = [(label, v) for label, v in results if label == document_types.OTHER]
    right = sum(v["verdict"] == "accepted" and v["key"] == label for label, v in typed)
    wrong = sum(v["verdict"] == "accepted" and v["key"] != label for label, v in typed)
    false_positive = sum(v["verdict"] == "accepted" for _, v in others)
    accepted = right + wrong + false_positive
    quote_refused = sum(v.get("reason") == "quote not in document" for _, v in results)
    return {
        "items": len(results),
        "recall": round(100 * right / len(typed), 1) if typed else 0.0,
        "precision": round(100 * right / accepted, 1) if accepted else 100.0,
        "wrong_type": wrong,
        "false_positive_on_other": false_positive,
        "quote_refused": quote_refused,
    }


def run(user, app):
    """Every pack item through production's prompt and checks. Paid, one call an item."""
    from .ai import invoke_ai
    from .models import AIConfiguration

    config = AIConfiguration.objects.filter(
        application=app, purpose="document_typing", enabled=True
    ).first()
    pack = load_pack()
    results, details = [], []
    for item in pack["items"]:
        receipt = {}
        answer = invoke_ai(
            user, app.pk, "document_typing", "What kind of document is this?",
            [{"id": item["id"], "title": "", "excerpt": item["text"]}],
            instructions=document_ai.TYPING_INSTRUCTIONS,
            max_tokens=document_ai.TYPING_TOKENS,
            receipt=receipt,
        )
        verdict = document_ai.judge_type(document_ai.parse(answer), item["text"])
        results.append((item["label"], verdict))
        details.append({
            "id": item["id"],
            "label": item["label"],
            "verdict": verdict["verdict"],
            "key": verdict["key"],
            "reason": verdict.get("reason", ""),
            "proposed": verdict.get("proposed", ""),
            "quotes": [quote["text"] for quote in verdict.get("quotes", [])],
            "tokens": [receipt.get("prompt_tokens"), receipt.get("completion_tokens")],
        })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": config.model if config else "",
        "provider": config.provider if config else "",
        "registry_version": pack.get("registry_version"),
        "prompt_version": document_ai.TYPING_VERSION,
        "metrics": score(results),
        "details": details,
    }


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
