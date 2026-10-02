"""Code Factory after approval: the work order, the implementation, its tests,
the review, verification, delivery and what GitHub's checks made of it.

Split out of `code_factory` along the line the pipeline already draws: the
first three phases produce a description of work and stop, and everything here
writes - or prepares to write - somebody's repository behind its own gate. The
shared machinery (phases, notes, `ask`, the ground rules) stays in
`code_factory` and is imported from it; nothing there imports from here.
"""

import re
import time
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404
from django.utils import timezone

from .code_factory import (
    GROUND_RULES,
    PHASE_TIMEOUT,
    PHASE_TIMEOUTS,
    REVIEW_INSTRUCTIONS,
    TEST_INSTRUCTIONS,
    WORK_ORDER_INSTRUCTIONS,
    UnusableAnswer,
    ask,
    finish_phase,
    note,
    parse_json,
    start_phase,
)
from .models import (
    FactoryRun,
    RunPhase,
)
from .services import audit

#: Text a model leaves behind when it abbreviates instead of writing the file.
ELISIONS = ("... rest of", "# ...", "// ...", "<!-- ... -->", "unchanged ...", "... (")

IMPLEMENTATION_INSTRUCTIONS = GROUND_RULES + (
    "You are given the current contents of files from a repository, each with a "
    "number. An entry marked NEW FILE does not exist yet and has no contents: "
    "the approved work is to write it from nothing. Apply the approved changes "
    "described to you. Return only a JSON object with a files array, each entry "
    "having: file (the number of the file you are changing) and content (that "
    "file's complete new text). Return only files you are actually changing, and "
    "return each one in full - not a diff, not an excerpt, and never a "
    "placeholder or an elision. If a change cannot be made from what you were "
    "given, leave that file out and explain why in a notes string on the object. "
    "If the code writes something - an audit record, a status - and then raises "
    "an error, check in the reference how a failed request's transaction ends: "
    "if an error rolls it back, the write is lost unless it is committed first "
    "or kept outside that transaction. Keep every behaviour the existing tests in "
    "the reference check - an unknown record still not found, a permission still "
    "refused - unless an approved item deliberately changes it."
)


def write_credential(app):
    """The write-scoped token, separate from the read-only connector one.

    Different files on purpose: letting an application import issues must not
    also let it push. Absent is a state, not a failure - an application with no
    write credential simply cannot deliver, and is told so.
    """
    from .secrets import application_secret

    return application_secret(app, "github_write")


#: A path the design annotated with the symbol it means: "app/views.py (index)".
#: The prompt asks for files, and naming the function inside one is a reasonable
#: reading of it - but the annotation is not part of the path, and left on it the
#: file is not found, is therefore taken to be absent, and is *created* under
#: that name. Stripped rather than refused: the path itself is exactly right.
ANNOTATED = re.compile(r"^(?P<path>[^()]*[^()\s])\s*\([^()]*\)$")

#: The same mistake in the other notations a model reaches for: "errors.py:Consent",
#: "views.py::index", "app.py:42", "app.py#L10". Only a suffix after something that
#: ends in a file extension is taken off, so a name that merely contains a colon
#: is left alone. A live run wrote all three of its files as new files named
#: "…/errors.py:ConsentWithheld" and edited none of the real ones.
SYMBOL_SUFFIX = re.compile(r"^(?P<path>[^:#\s]+\.[A-Za-z0-9]{1,8})(?:::?|#)[^/\s]+$")


def target_paths(plan):
    """Every repository path the approved items named, in order, deduplicated."""
    seen, paths = set(), []
    for item in plan.items.exclude(status="rejected").order_by("sequence"):
        for target in item.targets:
            candidate = str(target).strip()
            annotated = ANNOTATED.match(candidate)
            if annotated:
                candidate = annotated.group("path").strip()
            suffixed = SYMBOL_SUFFIX.match(candidate)
            if suffixed:
                candidate = suffixed.group("path")
            if not candidate or candidate in seen:
                continue
            # A target is only a path if it looks like one. Design is allowed to
            # name a component instead, and that is not something to open.
            if "/" in candidate or "." in candidate:
                seen.add(candidate)
                paths.append(candidate)
    return paths


#: Shown in place of contents for a path the approved plan named that is not
#: there. A fix is not only an edit: the change that proves a bug is fixed is
#: usually a test file that does not exist yet.
NEW_FILE = "NEW FILE. This path does not exist in the repository. Write it in full."


def numbered_files(files):
    """Files as the model sees them: by number, never by path.

    The model that writes contents must not be able to introduce a path. It
    answers by the number it was shown, and the path comes from what we read.
    """
    return [
        {
            "id": str(index),
            "title": found["path"],
            # Text, not a sha, decides this. A file read from the offline
            # snapshot has no blob sha, and was shown as NEW FILE - so an
            # offline implementation rewrote existing files from nothing.
            "excerpt": found["text"][:20000] if (found["sha"] or found["text"]) else NEW_FILE,
            "digest": found["sha"] or "",
        }
        for index, found in enumerate(files, start=1)
    ]


def approved_summary(plan):
    return "\n\n".join(
        f"{item.title}\n{item.change_summary}"
        for item in plan.items.exclude(status="rejected").order_by("sequence")
    )[:8000]


def run_work_order(run, token):
    """Turn the approved items into an instruction per file.

    The first agent of the implementation half, and the only one that reads the
    approved items as a whole. Implementation then works one file at a time
    against an intent somebody can check it against afterwards, which is what
    makes the review agent possible: without a stated intent there is nothing to
    review a file against except the reviewer's own opinion of the ticket.
    """
    started = time.monotonic()
    phase = start_phase(run, "work_order")
    try:
        files = readable_targets(run, token)
    except ValidationError as failure:
        finish_phase(phase, "failed", started, error=" ".join(failure.messages))
        raise
    note(
        run,
        f"Work order: deciding what each of the {len(files)} file(s) must end up doing.",
        phase="work_order",
    )
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "work_order",
            WORK_ORDER_INSTRUCTIONS,
            f"Approved changes:\n{approved_summary(run.plan)}",
            numbered_files(files),
            receipt,
        )
        payload = parse_json(answer, "Work order")
    except ValidationError as failure:
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        raise
    order = {}
    for entry in (payload.get("files") or [])[: len(files)]:
        if not isinstance(entry, dict):
            continue
        order[str(entry.get("file"))] = {
            "intent": str(entry.get("intent") or "")[:600],
            "checks": [str(check)[:200] for check in (entry.get("checks") or [])[:8]],
        }
    leftover = [str(item)[:200] for item in (payload.get("leftover") or [])[:8]]
    finish_phase(
        phase, "ok", started, output={"order": order, "leftover": leftover}, usage=receipt
    )
    note(
        run,
        f"Work order: {len(order)} file(s) have an intent to meet"
        + (f". {len(leftover)} approved item(s) no file can satisfy." if leftover else "."),
        phase="work_order",
        level="result",
    )
    for missed in leftover:
        note(
            run,
            f"Not implementable as a code change: {missed}",
            phase="work_order",
            level="check",
        )
    return files, order


def snapshot_targets(run, wanted):
    """The named files as the pinned Code Graph snapshot holds them.

    The offline path. Nothing here reaches GitHub, so it works in an
    environment that cannot, and the contents are exactly what the snapshot was
    indexed from - a known commit, named on the run.

    A file carries no blob sha, because the snapshot does not have one and
    inventing one would tell verification it may skip a check it cannot make.
    Everything downstream therefore treats these as new files, which is the
    safe reading: nothing is overwritten on the strength of a sha nobody has.
    """
    if not run.code_snapshot_id:
        raise ValidationError(
            "This run has no code snapshot and no write credential, so there is "
            "nothing to read the named files from. Index the repository in Code "
            "Graph, or mount a write-scoped credential."
        )
    if run.code_snapshot.repository.provider == "local":
        message = (
            f"The agents read {run.code_snapshot.repository.name} snapshot "
            f"v{run.code_snapshot.number}, indexed from a folder on this server. "
            "GitHub is not contacted, and nothing is written until somebody chooses to."
        )
    else:
        message = (
            f"No write credential is mounted, so the agents read "
            f"{run.code_snapshot.repository.name} snapshot v{run.code_snapshot.number} "
            f"at commit {run.code_snapshot.commit_sha[:8]} instead of the live "
            "repository. Nothing will be written anywhere."
        )
    note(run, message, phase="work_order", level="check")
    held = {item.path: item for item in run.code_snapshot.files.filter(path__in=wanted)}
    files = []
    for path in wanted:
        found = held.get(path)
        if found is None:
            note(
                run,
                f"{path} is not in the snapshot. It is treated as a file to write.",
                phase="work_order",
            )
            files.append({"path": path, "text": "", "sha": None})
            continue
        files.append({"path": path, "text": found.content, "sha": None})
        note(run, f"Read {path} from the snapshot.", phase="work_order")
    if not files:
        raise ValidationError(
            "None of the files the approved items named are in the pinned snapshot."
        )
    return files


def readable_targets(run, token):
    """Every file the approved items named, read once for the whole chain.

    Read here rather than inside implementation because three agents need them:
    the work order decides what each must do, implementation writes them, and
    the review agent reads the result. Reading them once also means one set of
    "I read this at sha X" facts, which is what verification re-checks.

    A named path that is not in the repository becomes a file to write rather
    than one to skip. That is safe here and nowhere else in the pipeline: the
    path was named by a plan item a second person approved, and no model ever
    sees a path -- each answers by the number its entry was shown with.
    """
    from .github_write import MAX_FILES, absent_path, read_file

    wanted = target_paths(run.plan)[:MAX_FILES]
    if not token:
        return snapshot_targets(run, wanted)
    note(
        run,
        f"Reading {len(wanted)} file(s) the approved items named from "
        f"{run.proposed_repository} at {run.base_branch}.",
        phase="work_order",
    )
    files = []
    for path in wanted:
        found = read_file(run.proposed_repository, path, run.base_branch, token)
        if found:
            files.append(found)
            note(run, f"Read {found['path']}.", phase="work_order")
            continue
        missing = absent_path(run.proposed_repository, path, run.base_branch, token)
        if missing:
            files.append({"path": missing, "text": "", "sha": None})
            note(
                run,
                f"{missing} is not in the repository. It will be created.",
                phase="work_order",
            )
        else:
            note(
                run,
                f"{path} could not be read and is not simply absent. It is left alone.",
                phase="work_order",
                level="check",
            )
    if not files:
        raise ValidationError(
            "None of the files the approved items named could be read from "
            f"{run.proposed_repository} at {run.base_branch}, and none of them are "
            "paths that are simply not there. Nothing was changed."
        )
    return files


#: How much read-only reference the implementation is given: enough to call the
#: code a change depends on correctly, not so much that it drowns the targets.
REFERENCE_FILES = 10
REFERENCE_BYTES = 64_000
REFERENCE_FILE_BYTES = 12_000

#: File stems too common to mean a specific module when a gap uses the word.
GENERIC_STEMS = {
    "__init__", "__main__", "main", "app", "base", "common", "config", "core",
    "helpers", "index", "models", "settings", "types", "utils", "util", "views",
}


def reference_files(run, targets):
    """Files the change depends on but does not change, from the pinned snapshot.

    The implementation used to see only the files the design named. A live run
    whose design named the export route but not the consent service it had to
    call was asked to add a consent check without being shown how consent is
    read - and, correctly, declined to invent one, twice. So it is also shown,
    read-only:

    * modules the approved items name by word - a gap about "consent" brings
      `services/consent.py` - because the code a fix must start calling is, by
      definition, not imported yet; and
    * what the target files already import, call or extend, from the
      snapshot's edges.

    Chosen by code from paths the snapshot holds, never by the model. Returned
    outside the numbered files, so `collect_changes` cannot accept an edit to
    one: they are context, and the change stays what the plan approved.
    """
    from .models import CodeRelationship

    snapshot = run.code_snapshot if run.code_snapshot_id else None
    if snapshot is None:
        return []
    ordered_targets = [item["path"] for item in targets]
    targets = set(ordered_targets)
    text = " ".join(
        f"{item.title} {item.explanation} {item.change_summary}"
        for item in run.plan.items.exclude(status="rejected")
    ).lower()
    paths = list(snapshot.files.values_list("path", flat=True))
    named = []
    for path in paths:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if (
            path in targets
            or looks_like_test(path)
            or len(stem) < 4
            or stem in GENERIC_STEMS
            or not re.search(rf"\b{re.escape(stem)}\b", text)
        ):
            continue
        named.append(path)
    # The first target's imports first: it is the file the change is about,
    # and its dependencies are the ones the new code will sit beside. Then their
    # imports, one level further. What decides a request's behaviour is often
    # two steps away: a live run's route reached the database only through its
    # dependency module, never saw that an error rolls the request's
    # transaction back, and wrote an audit record the rollback then discarded.
    # Calls and inheritance count as well as imports: in Java, Go or C# a file
    # uses its own package without importing it, so imports alone reach almost
    # nothing there. Those edges come from Graphify (`code_graph_graphify`).
    edges = {}
    for source, target in CodeRelationship.objects.filter(
        snapshot=snapshot, kind__in=["import", "call", "inherit"]
    ).values_list("source__path", "target__path"):
        edges.setdefault(source, []).append(target)

    def specific(path):
        return path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() not in GENERIC_STEMS

    first = [
        path
        for source in ordered_targets
        for path in sorted(edges.get(source, []))
        if specific(path)
    ]
    second = [
        path
        for source in dict.fromkeys(first)
        for path in sorted(edges.get(source, []))
        if specific(path)
    ]
    imported = list(dict.fromkeys(first + second))
    covering = list(covering_tests(snapshot, ordered_targets))
    support = test_support(snapshot, covering)
    chosen, spent = [], 0
    everything = covering + support + named + imported
    held = {item.path: item for item in snapshot.files.filter(path__in=everything)}
    # Tests first: they say what the change must keep true, which is the one
    # thing a design that forgot to mention them cannot recover.
    for path in dict.fromkeys(everything):
        found = held.get(path)
        if found is None or path in targets:
            continue
        text_part = found.content[:REFERENCE_FILE_BYTES]
        limit = REFERENCE_FILES + len(covering) + len(support)
        if len(chosen) >= limit or spent + len(text_part) > REFERENCE_BYTES:
            break
        chosen.append(
            {
                "path": path,
                "text": text_part,
                "named": path in named,
                "test": path in covering,
                "support": path in support,
            }
        )
        spent += len(text_part)
    return chosen


#: Prefixes a route or view module carries that its test file often drops:
#: `routes_fhir.py` is tested by `test_fhir.py`.
MODULE_PREFIXES = ("routes_", "route_", "views_", "view_", "api_", "handlers_")

#: Files that define what tests may use - fixtures, clients, setup - without
#: being tests themselves. A test author not shown these invents fixtures.
SUPPORT_NAMES = ("conftest.py",)
SUPPORT_PREFIXES = ("setupTests.", "jest.setup.", "vitest.setup.", "test_helpers.", "helpers.")


def test_support(snapshot, tests):
    """The fixture and setup files the given tests can see, nearest first.

    pytest collects every conftest.py from the test's directory up to the root,
    so those are the ones a test can use; for a repository with no covering test
    yet, the top-level test directories' support files stand in.
    """
    paths = list(snapshot.files.values_list("path", flat=True))

    def is_support(path):
        name = path.rsplit("/", 1)[-1]
        return name in SUPPORT_NAMES or name.startswith(SUPPORT_PREFIXES)

    support = [path for path in paths if is_support(path)]
    folders = {test.rsplit("/", 1)[0] if "/" in test else "" for test in tests}
    if not folders:
        folders = {path.rsplit("/", 1)[0] for path in support if path.count("/") <= 1}
    chosen = []
    for folder in sorted(folders, key=len, reverse=True):
        parts = folder.split("/") if folder else []
        for depth in range(len(parts), -1, -1):
            prefix = "/".join(parts[:depth])
            for path in support:
                if (path.rsplit("/", 1)[0] if "/" in path else "") == prefix and path not in chosen:
                    chosen.append(path)
    return chosen[:2]
COVERING_TESTS = 3


def covering_tests(snapshot, targets):
    """Existing test files that exercise the target files, from the snapshot.

    Found three ways, because a test often reaches its subject over HTTP and
    imports nothing of it: the test's name (`test_fhir.py` for `routes_fhir.py`),
    the module's name in the test, and a distinctive literal segment of a route
    the target declares (`$everything`). Returns {test path: [targets covered]}.

    A live run changed an endpoint without being shown its existing tests, so
    it could neither keep them passing nor update the one the ticket made
    wrong; CI then failed on both.
    """
    if snapshot is None:
        return {}
    wanted = {item for item in targets if not looks_like_test(item)}
    held = {item.path: item for item in snapshot.files.filter(path__in=wanted)}
    tests = [item for item in snapshot.files.all() if looks_like_test(item.path)]

    def rare(needle, pattern=None):
        """Whether a clue is specific: found in at most two test files.

        "Patient" or "audit" appear across a whole suite and pick nothing out;
        a first live try matched half of one and missed the test that mattered.
        """
        hits = sum(
            1
            for test in tests
            if (re.search(pattern, test.content) if pattern else needle in test.content)
        )
        return 0 < hits <= 2

    scores, found = {}, {}
    for target in wanted:
        stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        short = next(
            (stem[len(prefix):] for prefix in MODULE_PREFIXES if stem.startswith(prefix)),
            stem,
        )
        names = {f"test_{stem}", f"test_{short}", f"{stem}_test", f"{short}_test"}
        word = rf"\b{re.escape(stem)}\b"
        stem_is_clue = stem.lower() not in GENERIC_STEMS and rare(stem, word)
        segments = set()
        source = held.get(target)
        for route in (source.routes if source else None) or []:
            for part in str(route.get("raw") or "").split("/"):
                if len(part) >= 4 and "{" not in part and "*" not in part and rare(part):
                    segments.add(part)
        for test in tests:
            test_stem = test.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            score = (
                3 * (test_stem in names)
                + 2 * bool(stem_is_clue and re.search(word, test.content))
                + 2 * any(segment in test.content for segment in segments)
            )
            if score >= 2:
                scores[test.path] = scores.get(test.path, 0) + score
                found.setdefault(test.path, []).append(target)
    ranked = sorted(scores, key=lambda path: (-scores[path], path))[:COVERING_TESTS]
    return {path: sorted(found[path]) for path in ranked}


def reference_block(snapshot, references):
    """The read-only files as prompt text, labelled so they are not mistaken."""
    if not references:
        return ""
    parts = [
        f"READ-ONLY REFERENCE from snapshot v{snapshot.number} at "
        f"{snapshot.commit_sha[:8]}. These files are not yours to change and must "
        "not be returned. Use them to call existing code as it really is - its "
        "names, arguments and return values - instead of guessing:"
    ]
    for item in references:
        label = (
            "EXISTING TEST - keep it passing; where the approved change deliberately "
            "alters what it asserts, the test author will update it"
            if item.get("test")
            else "TEST SUPPORT - the fixtures and setup tests may use"
            if item.get("support")
            else ""
        )
        parts.append(f"--- {item['path']}{' (' + label + ')' if label else ''} ---\n{item['text']}")
    return "\n\n".join(parts)


def run_implementation(run, token, files, order=None, references=None):
    """Write the new contents of each file, against the intent set for it.

    The model is shown the work order's intent above each file's current
    contents, so it is answering "make this file do X" rather than re-reading
    the whole ticket per file. It is also shown, read-only, the code those
    files depend on (`reference_files`).
    """
    started = time.monotonic()
    phase = start_phase(run, "implementation")
    numbered = numbered_files(files)
    if references is None:
        references = reference_files(run, files)
    if references:
        note(
            run,
            "Implementation: also shown, read-only, "
            + ", ".join(item["path"] for item in references)
            + " - code the change depends on but does not change.",
            phase="implementation",
        )
    for entry in numbered:
        intent = (order or {}).get(entry["id"])
        if not intent:
            continue
        checks = "; ".join(intent["checks"])
        entry["excerpt"] = "\n".join(
            part
            for part in (
                f"MUST END UP: {intent['intent']}",
                f"CONFIRMABLE: {checks}" if checks else "",
                entry["excerpt"],
            )
            if part
        )
    note(
        run,
        f"Implementation: writing the new contents of {len(numbered)} file(s).",
        phase="implementation",
    )
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "implementation",
            IMPLEMENTATION_INSTRUCTIONS,
            "\n\n".join(
                part
                for part in (
                    f"Approved changes:\n{approved_summary(run.plan)}",
                    reference_block(run.code_snapshot, references),
                )
                if part
            ),
            numbered,
            receipt,
        )
        payload = parse_json(answer, "Implementation")
        changes = collect_changes(payload, files)
    except ValidationError as failure:
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        raise
    # New means the agents were shown no contents for it, not that it carried
    # no blob sha: a file read from a snapshot has none, and two edited files
    # were reported as "2 of them new".
    existing = {item["path"] for item in files if item.get("text")}
    finish_phase(
        phase,
        "ok",
        started,
        output={
            "notes": str(payload.get("notes") or "")[:1000],
            "references": [item["path"] for item in references],
            "files": [
                {
                    "path": change["path"],
                    "bytes": len(change["content"]),
                    "new": change["path"] not in existing,
                }
                for change in changes
            ],
        },
        usage=receipt,
    )
    created = [change["path"] for change in changes if change["path"] not in existing]
    note(
        run,
        f"Implementation returned {len(changes)} changed file(s)"
        + (f", {len(created)} of them new: {', '.join(created)}." if created else "."),
        phase="implementation",
        level="result",
    )
    return changes



#: Where tests live, by the conventions this platform can recognise. A change
#: to a file under one of these is a test change; anything else is not, which is
#: what stops the test agent from being handed the code it is meant to check.
TEST_MARKERS = ("test_", "_test.", "/tests/", "/test/", "spec.", ".spec.")


def looks_like_test(path):
    lowered = path.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


#: Source extensions a test path can be derived for, and how that language's
#: tests are usually named. Anything else is left to a plan that names its test.
PYTHON_SOURCE = (".py",)
SCRIPT_SOURCE = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
JAVA_SOURCE = (".java",)
#: Maven and Gradle's shared layout: a class's test sits at the same package
#: path under the test tree. Java code outside it gets no derived test.
JAVA_MAIN = "src/main/java/"
JAVA_TEST = "src/test/java/"


def derived_test_paths(changes, known):
    """A test file for each changed source file, when the plan named none.

    The design phase is asked for test files and does not always name one, and
    a change with no test is the weaker change. So the paths are chosen here, by
    code, from the repository's own layout - never by the model, which can only
    fill in the files it is shown (`collect_changes` matches its answer back by
    number). An existing test for the same module is extended rather than a
    second one created beside it.

    `known` is every path the repository is known to hold (the pinned snapshot's
    files); with none, the conventional default for the language is used.
    """
    import posixpath
    from collections import Counter as Tally

    tests = [path for path in known if looks_like_test(path)]
    by_name = {}
    for path in tests:
        by_name.setdefault(posixpath.basename(path).lower(), path)
    python_home = Tally(
        posixpath.dirname(path)
        for path in tests
        if posixpath.basename(path).startswith("test_") and path.endswith(".py")
    ).most_common(1)
    python_dir = python_home[0][0] if python_home else "tests"
    chosen = []
    for change in changes:
        path = change["path"]
        if looks_like_test(path):
            continue
        folder, name = posixpath.split(path)
        stem, extension = posixpath.splitext(name)
        if not stem or stem.startswith("__"):
            continue
        if extension in PYTHON_SOURCE:
            candidates = [f"test_{stem}.py", f"{stem}_test.py"]
            default = posixpath.join(python_dir, f"test_{stem}.py")
        elif extension in SCRIPT_SOURCE:
            candidates = [f"{stem}.test{extension}", f"{stem}.spec{extension}"]
            default = posixpath.join(folder, f"{stem}.test{extension}")
        elif extension in JAVA_SOURCE:
            module, marker, package = path.rpartition(JAVA_MAIN)
            if not marker or (module and not module.endswith("/")):
                continue
            # Matched on the whole path, not the name: two packages may both
            # hold an OrderService, and extending the other one's test is wrong.
            base = module + JAVA_TEST + posixpath.dirname(package)
            existing = next(
                (
                    known_path
                    for known_path in (
                        posixpath.join(base, f"{stem}Test.java"),
                        posixpath.join(base, f"{stem}Tests.java"),
                    )
                    if known_path in tests
                ),
                None,
            )
            target = existing or posixpath.join(base, f"{stem}Test.java")
            if target not in chosen:
                chosen.append(target)
            continue
        else:
            continue
        existing = next((by_name[c.lower()] for c in candidates if c.lower() in by_name), None)
        target = existing or default
        if target not in chosen:
            chosen.append(target)
    return chosen


def repository_test_setup(run, token):
    """The run's repository's test framework and CI, as `repo_testing` reads them.

    Live when there is a credential, since the default branch may have moved on
    since indexing; otherwise what the pinned snapshot recorded.
    """
    from . import github_write
    from .repo_testing import detect

    if token and run.proposed_repository:

        def read(path):
            found = github_write.read_file(run.proposed_repository, path, run.base_branch, token)
            return found["text"] if found else None

        def listdir(path):
            return github_write.list_directory(
                run.proposed_repository, path, run.base_branch, token
            )

        try:
            return detect(read, listdir)
        except ValidationError:
            return {}
    if run.code_snapshot_id:
        return dict(run.code_snapshot.test_setup or {})
    return {}


def test_language(paths):
    """The language the test files are in, for choosing a framework line.

    None when they are in none of the languages `repo_testing` reads a
    framework for; the line then says to follow the tests already there, rather
    than naming a Python or JavaScript runner for, say, a Ruby spec.
    """
    for language, extensions in (
        ("python", PYTHON_SOURCE),
        ("javascript", SCRIPT_SOURCE),
        ("java", JAVA_SOURCE),
    ):
        if any(path.endswith(extensions) for path in paths):
            return language
    return None


def run_tests_agent(run, changes, token, references=None):
    """Write the tests that would fail before this change and pass after it.

    Given the files *as they will be*, not as they are: a test written against
    the old contents would be testing the bug. The existing test files are shown
    alongside so the new ones follow the conventions already there rather than
    inventing a framework the repository does not use.

    A failure here does not stop the run. A change with no new test is worse
    than one with them and better than nothing, and the reviewer is told which
    it is - refusing the whole change because the test agent stumbled would
    throw away work that is already correct.
    """
    from .github_write import MAX_FILES, absent_path, read_file

    started = time.monotonic()
    phase = start_phase(run, "tests")
    wanted = [path for path in target_paths(run.plan) if looks_like_test(path)][:MAX_FILES]
    # Whether these paths are the test author's own choice rather than the
    # plan's. Carried on each file it writes, so review can drop a test that
    # would otherwise go out without the code it was written for.
    chosen_here = not wanted
    if not wanted:
        known = (
            list(run.code_snapshot.files.values_list("path", flat=True))
            if run.code_snapshot_id
            else []
        )
        wanted = derived_test_paths(changes, known)[:MAX_FILES]
        if wanted:
            note(
                run,
                "Test author: no approved item named a test file, so tests are written "
                f"for the changed code at {', '.join(wanted)}, following the "
                "repository's own layout.",
                phase="tests",
            )
    # Tests that already cover the changed code: the test author may update
    # them where the approved change makes an assertion wrong. Like a derived
    # path, each exists here only because of the changed code, so review drops
    # its update if the code it covers is rejected.
    covers = covering_tests(
        run.code_snapshot if run.code_snapshot_id else None,
        [change["path"] for change in changes],
    )
    covering = [path for path in covers if path not in wanted][:MAX_FILES]
    if covering:
        note(
            run,
            "Test author: existing tests cover the changed code and may be updated "
            f"where the change makes them wrong: {', '.join(covering)}.",
            phase="tests",
        )
    for_changed_code = (set(wanted) if chosen_here else set()) | set(covering)
    wanted = (wanted + covering)[:MAX_FILES]
    if not token:
        # The offline path reads the same snapshot the other agents did.
        existing = snapshot_targets(run, wanted) if wanted else []
    else:
        existing = []
        for path in wanted:
            found = read_file(run.proposed_repository, path, run.base_branch, token)
            if found:
                existing.append(found)
                continue
            missing = absent_path(run.proposed_repository, path, run.base_branch, token)
            if missing:
                existing.append({"path": missing, "text": "", "sha": None})
    # A test file the implementation already changed is shown as the
    # implementation left it, still carrying the sha it was read at. The test
    # author then builds on that rather than on the old file, and its version
    # replaces the implementation's instead of sitting beside it.
    already = {change["path"]: change for change in changes}
    existing = [
        {**item, "text": already[item["path"]]["content"]} if item["path"] in already else item
        for item in existing
    ]
    if not existing:
        finish_phase(
            phase,
            "skipped",
            started,
            output={
                "reason": "No approved item named a test file, and none of the changed "
                "files is in a language a test path can be chosen for."
            },
        )
        note(
            run,
            "Test author: no approved item named a test file and no test path could be "
            "chosen for the changed files, so none were written.",
            phase="tests",
            level="check",
        )
        return []
    from .repo_testing import ci_summary, instruction

    setup = repository_test_setup(run, token)
    framework_line = instruction(setup, test_language([item["path"] for item in existing]))
    note(
        run,
        f"Test author: writing tests for {len(existing)} test file(s), against the "
        f"code as it will be after the change. {framework_line}",
        phase="tests",
    )
    from django.conf import settings

    if local_repository(run) is not None and settings.CODE_FACTORY_LOCAL_TESTS:
        note(
            run,
            "Test author: a folder has no CI. Once the change is written, run these "
            "tests on the Tests stage.",
            phase="tests",
        )
    elif setup.get("known") and not setup.get("ci"):
        note(run, f"Test author: {ci_summary(setup)}", phase="tests", level="problem")
    finished = "\n\n".join(
        f"FILE {change['path']}\n{change['content'][:8000]}" for change in changes
    )[:20000]
    receipt = {}
    payload = None
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "tests",
            TEST_INSTRUCTIONS,
            "\n\n".join(
                part
                for part in (
                    f"Approved changes:\n{approved_summary(run.plan)}",
                    framework_line,
                    f"The files as they will be:\n{finished}",
                    # The existing tests it may edit are numbered files already;
                    # everything else it may only read.
                    reference_block(
                        run.code_snapshot,
                        [item for item in references or [] if item["path"] not in wanted],
                    ),
                )
                if part
            ),
            test_entries(existing, covering),
            receipt,
        )
        payload = parse_json(answer, "Test author")
        written = collect_changes(payload, existing, agent="test author")
    except ValidationError as failure:
        if payload is not None and all(item["path"] in already for item in existing):
            # It answered, and every test file it was given the implementation had
            # already written; it had nothing to add. That is the tests being
            # done, not missing.
            finish_phase(
                phase,
                "ok",
                started,
                output={"files": [], "framework": framework_line, "setup": setup},
                usage=receipt,
            )
            note(
                run,
                "Test author: the implementation already wrote "
                + ", ".join(item["path"] for item in existing)
                + ", and there was nothing to add.",
                phase="tests",
                level="result",
            )
            return []
        # Recorded and stepped over, not raised: see the docstring.
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        note(
            run,
            "Test author produced nothing usable. The change stands without new "
            "tests, and the review agent is told so.",
            phase="tests",
            level="problem",
        )
        return []
    finish_phase(
        phase,
        "ok",
        started,
        output={
            "files": [change["path"] for change in written],
            "framework": framework_line,
            "setup": setup,
        },
        usage=receipt,
    )
    note(
        run,
        f"Test author wrote {len(written)} test file(s): "
        + ", ".join(change["path"] for change in written),
        phase="tests",
        level="result",
    )
    for change in written:
        change["test_for_changed_code"] = change["path"] in for_changed_code
        change["covers"] = covers.get(change["path"], [])
    return written


def test_entries(existing, covering):
    """The test author's numbered files, with existing covering tests labelled."""
    entries = numbered_files(existing)
    for entry in entries:
        if entry["title"] in covering:
            entry["excerpt"] = (
                "EXISTING TEST covering the changed code. Update only what the "
                "approved change deliberately makes wrong; leave everything else as "
                "it is, or leave the file out.\n" + entry["excerpt"]
            )
    return entries


def run_review(run, changes, order, references=None):
    """A second model reading the finished change before anybody is asked to.

    It sees what a reviewer would: the approved items, and the files as they
    will be. A file it rejects is dropped from the change rather than the whole
    change being abandoned - the rest may be perfectly good, and a person still
    decides whether to open anything at all.
    """
    started = time.monotonic()
    phase = start_phase(run, "review")
    note(
        run,
        f"Change review: reading {len(changes)} finished file(s) against the "
        "approved items.",
        phase="review",
    )
    numbered = [
        {
            "id": str(index),
            "title": change["path"],
            "excerpt": (
                f"MUST END UP: {order.get(str(index), {}).get('intent', 'not stated')}\n"
                f"{change['content'][:20000]}"
            ),
            "digest": change["sha"] or "",
        }
        for index, change in enumerate(changes, start=1)
    ]
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "review",
            REVIEW_INSTRUCTIONS,
            "\n\n".join(
                part
                for part in (
                    f"Approved changes:\n{approved_summary(run.plan)}",
                    # Shown the original of anything not being changed, so it can
                    # check calls against real code and changes against real tests.
                    reference_block(
                        run.code_snapshot,
                        [
                            item
                            for item in references or []
                            if item["path"] not in {change["path"] for change in changes}
                        ],
                    ),
                )
                if part
            ),
            numbered,
            receipt,
        )
        payload = parse_json(answer, "Change review")
    except ValidationError as failure:
        # A review that cannot be read is not a pass, so the change stops here.
        # It used to go on to the mechanical checks unreviewed, with a note -
        # which is how a live run came to prepare, and then publish, a change
        # that was one test for code nobody had written. The mechanical checks
        # confirm paths and staleness; they cannot tell a wrong change from a
        # right one, and a note is easy to miss on the way to "Yes". Rerunning
        # the implementation agents is one button on the run.
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        note(
            run,
            "Change review produced nothing readable, so the change is stopped "
            "rather than sent on unreviewed.",
            phase="review",
            level="problem",
        )
        raise ValidationError(
            "The change review could not be read, so nothing was prepared: an "
            "unreviewed change is not offered for a pull request. Rerun the "
            "implementation agents; this is usually a one-off."
        ) from failure
    verdicts = {}
    for entry in (payload.get("files") or [])[: len(changes)]:
        if isinstance(entry, dict):
            verdicts[str(entry.get("file"))] = (
                str(entry.get("verdict") or "").lower(),
                str(entry.get("reason") or "")[:300],
            )
    kept, dropped = [], []
    for index, change in enumerate(changes, start=1):
        verdict, reason = verdicts.get(str(index), ("ok", ""))
        if verdict == "reject":
            dropped.append((change["path"], reason))
            note(
                run,
                f"Rejected {change['path']}: {reason or 'no reason given'}",
                phase="review",
                level="problem",
            )
            continue
        kept.append(change)
    orphaned = orphaned_tests(kept, [path for path, _ in dropped])
    for path, reason in orphaned:
        note(run, f"Dropped {path}: {reason}", phase="review", level="problem")
    kept = [change for change in kept if change["path"] not in dict(orphaned)]
    finish_phase(
        phase,
        "ok",
        started,
        output={
            "kept": [change["path"] for change in kept],
            "rejected": [{"path": path, "reason": reason} for path, reason in dropped],
            "orphaned": [{"path": path, "reason": reason} for path, reason in orphaned],
        },
        usage=receipt,
    )
    note(
        run,
        f"Change review: {len(kept)} file(s) passed"
        + (f", {len(dropped)} rejected and dropped from the change" if dropped else "")
        + (f", {len(orphaned)} test file(s) dropped with them" if orphaned else "")
        + ".",
        phase="review",
        level="result",
    )
    if not kept:
        raise ValidationError(
            "The change review rejected every file"
            + (", and the tests written for them went too" if orphaned else "")
            + ". Nothing is left to write."
        )
    return kept


def test_subject(path):
    """The module a test file is for, by the naming `derived_test_paths` uses."""
    import posixpath

    name = posixpath.basename(path)
    stem, extension = posixpath.splitext(name)
    if extension in PYTHON_SOURCE:
        return stem.removeprefix("test_").removesuffix("_test")
    if extension in SCRIPT_SOURCE:
        return stem.removesuffix(".test").removesuffix(".spec")
    return ""


def orphaned_tests(kept, rejected):
    """Test files that would go out without the code they test.

    A reviewer rejecting a file used to drop that file and keep the tests
    written for it, so a live run's change came to consist of one test file
    checking a consent guard that was never written - a test certain to fail,
    presented as the whole change.

    Only tests the test author wrote to paths it chose itself are candidates
    (`test_for_changed_code`): those exist only because of the changed code. One
    goes when the file it tests, matched by name, was rejected, or when no
    source file survives at all. Tests the plan named are always kept - a plan
    whose change *is* tests is a real plan - and so is anything the
    implementation wrote.
    """
    import posixpath

    rejected_subjects = {
        posixpath.splitext(posixpath.basename(path))[0]
        for path in rejected
        if not looks_like_test(path)
    }
    source_left = any(not looks_like_test(change["path"]) for change in kept)
    orphaned = []
    for change in kept:
        path = change["path"]
        if not change.get("test_for_changed_code"):
            continue
        if test_subject(path) in rejected_subjects or set(change.get("covers") or []) & set(
            rejected
        ):
            orphaned.append((path, "it tests a file the review rejected."))
        elif not source_left:
            orphaned.append((path, "it was written for changed code, and none is left."))
    return orphaned


def collect_changes(payload, files, agent="implementation"):
    """The proposed new contents, matched back to the entries we showed.

    A change carries the blob sha it was read at, or None when the entry was a
    path that is not in the repository. That sha is what tells the two later
    steps apart: verification re-reads one and re-checks the other is still
    absent, and delivery replaces one and creates the other.

    An entry names its file by the number it was shown with, or by that file's
    exact path - models do both. Either way it must be one of the files shown:
    a path is only ever looked up, never taken from the answer. Every entry is
    read (up to four per file shown), not only the first as many as were shown:
    a live test author returned the two source files it was not given, then the
    two test files it was, and cutting the list at two threw away the only
    entries that counted. One change per file; a repeat is ignored.
    """
    from .github_write import MAX_FILE_BYTES

    by_number = {str(index): found for index, found in enumerate(files, start=1)}
    by_path = {found["path"]: found for found in files}
    changes, dropped, taken = [], [], set()
    entries = payload.get("files") or []
    for entry in entries[: len(files) * 4]:
        if not isinstance(entry, dict):
            dropped.append("an entry that is not an object")
            continue
        key = str(entry.get("file"))
        found = by_number.get(key) or by_path.get(key)
        content = entry.get("content")
        if found is None:
            dropped.append(
                f"file {entry.get('file')!r} is not one of the {len(files)} shown "
                f"(keys: {', '.join(sorted(map(str, entry)))[:80]})"
            )
            continue
        if not isinstance(content, str) or not content.strip():
            dropped.append(f"{found['path']}: no content")
            continue
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            dropped.append(f"{found['path']}: larger than {MAX_FILE_BYTES} bytes")
            continue
        if found["path"] in taken:
            dropped.append(f"{found['path']}: returned twice")
            continue
        if content == found["text"]:
            # Returned unchanged: committing it would put an empty diff in front
            # of a reviewer and claim work that did not happen.
            dropped.append(f"{found['path']}: returned unchanged")
            continue
        taken.add(found["path"])
        changes.append({"path": found["path"], "content": content, "sha": found["sha"]})
    if not changes:
        # Say why. This used to be one fixed sentence, which threw away the
        # model's own account: asked to leave a file out and explain in `notes`
        # when a change cannot be made, it did exactly that, and the run said
        # only "no usable file changes".
        notes = str(payload.get("notes") or "").strip()
        message = f"The {agent} returned no usable file changes."
        if notes:
            message += f" It said: {notes[:700]}"
        elif not entries:
            message += " Its answer had no files in it and gave no reason."
        raise UnusableAnswer(
            message,
            sample=(
                f"{len(entries)} file entr{'y' if len(entries) == 1 else 'ies'} returned"
                + (f"; dropped: {'; '.join(dropped)[:900]}" if dropped else "")
                + (f"; notes: {notes[:600]}" if notes else "")
            ),
        )
    return changes


def _path_problems(change):
    """Path safety and elisions, the checks that need no repository."""
    from .github_write import safe_path

    try:
        safe_path(change["path"])
    except ValidationError as failure:
        yield failure
    if any(marker in change["content"] for marker in ELISIONS):
        yield ValidationError("contains an elision where code should be.")


def run_verification(run, changes, token):
    """Check what is about to be written, before anything is written.

    There is no test suite to run: this platform holds knowledge about an
    application, not a checkout of it with a known build command. What it can do
    is refuse a change that is obviously not one - a path nobody read, an elision
    where code should be, a file that moved underneath us - and say plainly that
    it did no more than that rather than implying a green build.
    """
    from .github_write import absent_path, read_file, safe_path

    started = time.monotonic()
    phase = start_phase(run, "verification")
    note(
        run,
        f"Pre-write checks: checking {len(changes)} file(s) before anything is written.",
        phase="verification",
    )
    if not token:
        # Working from the snapshot: there is no live repository to re-read, so
        # the staleness check cannot be made. Said plainly rather than skipped
        # quietly, because "checked" would otherwise mean two different things.
        findings = [
            f"{change['path']}: {' '.join(failure.messages)}"
            for change in changes
            for failure in _path_problems(change)
        ]
        if findings:
            finish_phase(phase, "failed", started, error="; ".join(findings)[:2000])
            raise ValidationError("; ".join(findings))
        finish_phase(
            phase,
            "ok",
            started,
            output={
                "checked": [change["path"] for change in changes],
                "created": [],
                "limits": (
                    "Paths and elisions were checked. No staleness check was "
                    "possible: these files came from a pinned snapshot, not from "
                    "a live repository, and nothing is being written."
                ),
            },
        )
        note(
            run,
            "Pre-write checks passed on paths and elisions. Staleness could not "
            "be checked - the files came from a snapshot - and nothing is being "
            "written anywhere.",
            phase="verification",
            level="result",
        )
        return True
    findings = []
    for change in changes:
        try:
            safe_path(change["path"])
        except ValidationError as failure:
            findings.append(f"{change['path']}: {' '.join(failure.messages)}")
            continue
        if any(marker in change["content"] for marker in ELISIONS):
            findings.append(f"{change['path']}: contains an elision where code should be.")
        if change["sha"] is None:
            # The staleness check for a file being created: it was not there
            # when we looked, and it must still not be there now. Somebody
            # else adding it in between is exactly the case this catches.
            if absent_path(run.proposed_repository, change["path"], run.base_branch, token) is None:
                findings.append(f"{change['path']}: exists now, having been absent when read.")
            continue
        current = read_file(run.proposed_repository, change["path"], run.base_branch, token)
        if current is None:
            findings.append(f"{change['path']}: could not be re-read before writing.")
        elif current["sha"] != change["sha"]:
            findings.append(f"{change['path']}: changed in the repository since it was read.")
    if findings:
        finish_phase(phase, "failed", started, error="; ".join(findings)[:2000])
        raise ValidationError("; ".join(findings))
    finish_phase(
        phase,
        "ok",
        started,
        output={
            "checked": [change["path"] for change in changes],
            "created": [change["path"] for change in changes if change["sha"] is None],
            "limits": (
                "Paths, elisions and staleness were checked, and each new path was "
                "confirmed still absent. No build or test suite was run: this platform "
                "holds knowledge about the application, not a checkout."
            ),
        },
    )
    note(
        run,
        "Pre-write checks passed: every path is safe, nothing was elided, no file "
        "moved since it was read, and each new path is still absent. No build or "
        "test suite was run.",
        phase="verification",
        level="result",
    )
    return True


def run_delivery(run, changes, token):
    """Branch, commit each file, and open a draft pull request."""
    from .github_write import (
        BRANCH_PREFIX,
        branch_head,
        commit_file,
        create_branch,
        open_pull_request,
    )

    started = time.monotonic()
    phase = start_phase(run, "delivery")
    branch = f"{BRANCH_PREFIX}{run.ticket_external_id or 'change'}-{str(run.pk)[:8]}".lower()
    note(run, f"Delivery: creating branch {branch} from {run.base_branch}.", phase="delivery")
    try:
        head = branch_head(run.proposed_repository, run.base_branch, token)
        create_branch(run.proposed_repository, branch, head, token)
        for change in changes:
            creating = change["sha"] is None
            note(
                run,
                f"Committing {'new file ' if creating else ''}{change['path']}.",
                phase="delivery",
            )
            commit_file(
                run.proposed_repository,
                branch,
                change["path"],
                change["content"],
                change["sha"],
                f"{run.ticket_external_id or 'Change'}: "
                f"{'add' if creating else 'update'} {change['path']}",
                token,
                create=creating,
            )
        note(run, "Opening a draft pull request.", phase="delivery")
        url = open_pull_request(
            run.proposed_repository,
            branch,
            run.base_branch,
            run.plan.title,
            pull_request_body(run, changes),
            token,
        )
    except ValidationError as failure:
        finish_phase(phase, "failed", started, error=" ".join(failure.messages))
        raise
    finish_phase(phase, "ok", started, output={"branch": branch, "pull_request": url})
    note(run, f"Draft pull request opened: {url}", phase="delivery", level="result")
    note(
        run,
        "The repository's own tests now run against this branch. This platform "
        "wrote them and never runs them; GitHub does.",
        phase="delivery",
        level="check",
    )
    FactoryRun.objects.filter(pk=run.pk).update(pull_request_url=url, branch=branch)
    return url


def pull_request_body(run, changes=()):
    """What a reviewer on the repository side needs without opening this platform.

    New files are named rather than left for the reviewer to notice in the diff:
    adding one is the more consequential half of what this can do, and somebody
    deciding how closely to read should be told which it is before they start.
    """
    created = [change["path"] for change in changes if change["sha"] is None]
    lines = [
        f"Raised from {run.ticket_url or run.ticket_external_id}.",
        "",
        f"Analysed against published knowledge graph version {run.graph_version}."
        if run.graph_version
        else "No published knowledge graph was available for this analysis.",
        "",
        "## Approved items",
    ]
    for item in run.plan.items.exclude(status="rejected").order_by("sequence"):
        heading = item.get_category_display()
        if item.rubric:
            heading += f" · {item.get_rubric_display()}"
        lines.append(f"\n### {item.title}")
        lines.append(f"_{heading} · {item.get_severity_display()}_")
        lines.append(item.explanation)
        if item.change_summary:
            lines.append(f"\n**What changes:** {item.change_summary}")
        for citation in item.citations:
            lines.append(f"\n> {str(citation.get('excerpt', ''))[:400]}")
            origin = citation.get("origin_label", "")
            system = citation.get("system", "")
            if system and system not in origin:
                origin = f"{origin} · {system}" if origin else system
            suffix = f" ({origin})" if origin else ""
            lines.append(f">\n> -- {citation.get('title', '')}{suffix}")
    lines.append(
        "\n---\nOpened as a draft by Digital Brain. Every quotation above was verified "
        "against its source before this was raised. No build or test suite was run."
    )
    if created:
        lines.insert(
            len(lines) - 1,
            "\n**New files in this change:** "
            + ", ".join(f"`{path}`" for path in created),
        )
    return "\n".join(lines)


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
    try:
        prepare(run.acting_user, run.application_id, run.pk)
    except (ValidationError, PermissionDenied, Http404) as failure:
        reason = " ".join(getattr(failure, "messages", ["The request was refused."]))
        FactoryRun.objects.filter(pk=run.pk).exclude(status="prepared").update(
            status="failed", error=reason[:2000]
        )
    return True


def prepare(user, app_id, run_id):
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
    claimed = FactoryRun.objects.filter(
        pk=run.pk, status__in=["prepare_queued", "awaiting_review", "prepared", "failed"]
    ).update(status="preparing", error="")
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


def local_repository(run):
    """The server folder this run's code came from, or None for GitHub."""
    snapshot = run.code_snapshot
    if snapshot is None or snapshot.repository.provider != "local":
        return None
    return snapshot.repository


def local_targets(run, folder, changes):
    """[(absolute path, change)] once every change is safe to write, or a refusal.

    Checked as a whole before anything is written, so a refusal leaves the
    folder exactly as it was. The same rules a pull request is held to, in the
    terms a folder has:

    * a path stays inside the folder, and never into a hidden, dependency or
      build folder - `.git` above all;
    * a file the agents read from the snapshot must still say what the snapshot
      says, or somebody changed it since and this would overwrite their work;
    * a file the agents wrote as new must still be absent, for the same reason.
    """
    from .code_graph_local import _skipped

    held = dict(run.code_snapshot.files.values_list("path", "content"))
    planned, problems = [], []
    for change in changes:
        path = change["path"]
        parts = path.replace("\\", "/").split("/")
        if (
            not path
            or path.startswith(("/", "\\"))
            or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)
            or any(_skipped(part) for part in parts[:-1])
            or parts[-1].startswith(".")
        ):
            problems.append(f"{path} is not a path this platform writes.")
            continue
        target = folder.joinpath(*parts)
        try:
            resolved = target.resolve()
        except (OSError, RuntimeError):
            problems.append(f"{path} could not be resolved.")
            continue
        if not resolved.is_relative_to(folder) or target.is_symlink():
            problems.append(f"{path} leads outside {folder}.")
            continue
        if target.exists():
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                problems.append(f"{path} could not be read to check it.")
                continue
            if path not in held:
                problems.append(
                    f"{path} exists, but the agents wrote it as a new file without "
                    "reading it. Writing it would replace a file nobody looked at."
                )
            elif current != held[path]:
                problems.append(
                    f"{path} has changed since snapshot v{run.code_snapshot.number}. "
                    "Refresh the index and implement the change again."
                )
        elif path in held:
            problems.append(f"{path} was removed from the folder after the snapshot.")
        planned.append((target, change))
    if problems:
        raise ValidationError(problems)
    return planned


def _write_text(target, content):
    """Replace one file in a single step, keeping its line endings."""
    import os
    import tempfile

    newline = "\n"
    if target.exists() and b"\r\n" in target.read_bytes()[:65536]:
        newline = "\r\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".codefactory-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(content)
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def apply_local(user, app_id, run_id):
    """Write a prepared change into the folder its code was read from.

    The local counterpart of `publish`, for a repository Code Graph reads from
    a folder on this server. Off unless an operator sets
    `code_factory_local_write`, and refused under production. Like `publish` it
    re-reads the prepared files rather than taking them from the caller, so what
    lands is what was shown. Nothing is committed: the folder's own tools
    decide what happens next, and its git history is untouched.
    """
    from django.conf import settings

    from .code_graph_ingest import local_folder

    app, run, _ = gate(user, app_id, run_id)
    if not settings.CODE_FACTORY_LOCAL_WRITE:
        raise ValidationError(
            "Writing to a folder on this server is not enabled. Set "
            "code_factory_local_write = true in the configuration to allow it."
        )
    repository = local_repository(run)
    if repository is None:
        raise ValidationError("This run's code was not read from a folder on this server.")
    if run.delivered:
        raise ValidationError("This change has already been delivered.")
    if run.status != "prepared":
        raise ValidationError("Implement the change and read its summary first.")
    changes = [{"path": item.path, "content": item.content} for item in run.changes.all()]
    if not changes:
        raise ValidationError("This run has no prepared change to write.")
    folder = local_folder(repository)
    claimed = FactoryRun.objects.filter(pk=run.pk, status="prepared").update(
        status="delivering", error=""
    )
    if not claimed:
        raise ValidationError("This run is already being delivered.")
    run.refresh_from_db()
    note(run, f"Writing to {folder} approved by {user.get_username()}.", level="check")
    try:
        planned = local_targets(run, folder, changes)
    except ValidationError:
        note(run, "Nothing was written to the folder.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(status="prepared")
        raise
    try:
        for target, change in planned:
            _write_text(target, change["content"])
            note(run, f"Wrote {change['path']}.", phase="delivery")
    except OSError as failure:
        note(run, f"Writing stopped: {failure.strerror or failure}.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error="A file could not be written. See the run log."
        )
        raise ValidationError("A file could not be written. See the run log.") from None
    FactoryRun.objects.filter(pk=run.pk).update(
        status="delivered", delivered_to=str(folder)[:500], finished_at=timezone.now()
    )
    audit(
        user,
        "factory.change_written_to_folder",
        run.pk,
        app.product.portfolio.organization,
        details={"folder": str(folder), "files": len(planned)},
    )
    note(
        run,
        f"{len(planned)} file(s) written to {folder}. Nothing was committed; review "
        "and commit them with the folder's own tools.",
        level="result",
    )
    return folder


#: How long a folder's own test suite may run before it is stopped.
LOCAL_TEST_TIMEOUT = 300
#: What the run's check is called, so it reads as a run here and not as CI.
LOCAL_TEST_CHECK = "pytest · run on this server"


def folder_python(folder):
    """The folder's own virtual environment's interpreter, or None.

    Its own, never this server's: the tests need the folder's dependencies, and
    running them under the platform's interpreter would test against ours.
    """
    venv = folder / ".venv"
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def run_local_tests(user, app_id, run_id):
    """Run the folder's test suite once a change has been written into it.

    The one place this platform runs code, and it runs it only here: off unless
    `code_factory_local_tests` is set, refused under production, and only when
    a person with approval rights presses the button. The suite runs with the
    folder's own interpreter, a minimal environment - no secrets directory, no
    credential variables - and a time limit. Its verdict is recorded where a
    pull request's CI verdict would be, so the Tests stage reads the same.
    """
    import os
    import subprocess

    from django.conf import settings

    from .code_graph_ingest import local_folder

    app, run, _ = gate(user, app_id, run_id)
    if not settings.CODE_FACTORY_LOCAL_TESTS:
        raise ValidationError(
            "Running tests on this server is not enabled. Set "
            "code_factory_local_tests = true in the configuration to allow it."
        )
    repository = local_repository(run)
    if repository is None or not run.delivered_to:
        raise ValidationError("Write the change to the folder before running its tests.")
    folder = local_folder(repository)
    setup = run.code_snapshot.test_setup or {}
    if setup.get("python") != "pytest":
        raise ValidationError(
            "Only pytest suites can be run here so far, and this folder does not declare one."
        )
    python = folder_python(folder)
    if python is None:
        raise ValidationError(
            f"{repository.name} has no .venv to run its tests with. Create one in the "
            "folder and install its dependencies, then try again."
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "LANG"}
    }
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    FactoryRun.objects.filter(pk=run.pk).update(
        checks=[{"name": LOCAL_TEST_CHECK, "status": "in_progress", "conclusion": None}],
        checks_read_at=timezone.now(),
    )
    note(run, f"{user.get_username()} ran {repository.name}'s tests on this server.", level="check")
    started = time.monotonic()
    try:
        finished = subprocess.run(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"],
            cwd=folder,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LOCAL_TEST_TIMEOUT,
            check=False,
        )
        lines = [line for line in (finished.stdout or "").splitlines() if line.strip()]
        summary = lines[-1].strip("= ").strip() if lines else "pytest reported nothing."
        conclusion = "success" if finished.returncode == 0 else "failure"
        tail = "\n".join(lines[-12:])
    except subprocess.TimeoutExpired:
        summary = f"Stopped after {LOCAL_TEST_TIMEOUT} seconds."
        conclusion, tail = "timed_out", ""
    except OSError as failure:
        summary = f"The tests could not be started: {failure.strerror or failure}."
        conclusion, tail = "failure", ""
    seconds = round(time.monotonic() - started, 1)
    FactoryRun.objects.filter(pk=run.pk).update(
        checks=[
            {
                "name": LOCAL_TEST_CHECK,
                "status": "completed",
                "conclusion": conclusion,
                "summary": f"{summary} ({seconds}s)"[:300],
                "url": "",
            }
        ],
        checks_read_at=timezone.now(),
    )
    note(
        run,
        f"Tests {'passed' if conclusion == 'success' else conclusion}: {summary}"
        + (f"\n{tail}" if tail and conclusion != "success" else ""),
        phase="tests",
        level="result" if conclusion == "success" else "problem",
    )
    audit(
        user,
        "factory.local_tests_run",
        run.pk,
        app.product.portfolio.organization,
        details={"folder": str(folder), "conclusion": conclusion, "summary": summary[:200]},
    )
    return conclusion, summary


#: How much one before/after picture draws. Past this it stops being a picture
#: of the change and becomes the whole graph again.
PICTURE_KNOWLEDGE = 4
PICTURE_DEPENDENCIES = 6
PICTURE_ROW = 40
#: The drawing's width, and half a node box. Small on purpose: the two drawings
#: sit side by side, so every unit here is shared by both.
PICTURE_WIDTH = 480
HALF_BOX = 60


def shown_paths(columns):
    return [key for column in columns[1:] for _, key, _ in column]


def change_picture(read, before, after, touched, was, now, connections):
    """Two drawings of the same neighbourhood - before the run and after it.

    Three columns: knowledge items on the left, the files the run changed in
    the middle, the code they depend on on the right. Both drawings share every
    position, so what moved is the only thing that differs. Coordinates are
    computed here because a CSP of `style-src 'self'` leaves SVG attributes as
    the only way to place anything.
    """
    from .code_knowledge import links

    changed = touched[:4]
    seen, chosen = set(), []
    for row in connections:
        for item in row["items"]:
            label = item["node_label"]
            if item["node_id"] not in seen and label not in {c["label"] for c in chosen}:
                seen.add(item["node_id"])
                chosen.append({"id": item["node_id"], "label": label})
    chosen = chosen[:PICTURE_KNOWLEDGE]

    outgoing = [edge for edge in (was | now) if edge[0] in changed and edge[1] not in changed]
    linked_paths = {}
    for node in chosen:
        for snapshot, key in ((before, "before"), (after, "after")):
            found, _ = links(read, snapshot, node_id=node["id"])
            linked_paths.setdefault(node["id"], {})[key] = {item["path"] for item in found}
    any_linked = set().union(*(set().union(*paths.values()) for paths in linked_paths.values()))
    ranked = sorted(
        {edge[1] for edge in outgoing},
        key=lambda path: (
            not any(edge[1] == path for edge in now - was),
            path not in any_linked,
            path,
        ),
    )
    dependencies = ranked[:PICTURE_DEPENDENCIES]

    def short(text, size=17):
        text = text.removeprefix("#: ").strip()
        return text if len(text) <= size else text[: size - 1] + "…"

    columns = (
        [("knowledge", node["id"], node["label"]) for node in chosen],
        [("changed", path, path) for path in changed],
        [("code", path, path) for path in dependencies],
    )
    height = max(len(column) for column in columns) * PICTURE_ROW + 12
    xs = (HALF_BOX + 8, PICTURE_WIDTH // 2, PICTURE_WIDTH - HALF_BOX - 8)
    existed = set(before.files.filter(path__in=shown_paths(columns)).values_list("path", flat=True))
    places, nodes = {}, []
    for column, x in zip(columns, xs, strict=True):
        top = (height - len(column) * PICTURE_ROW) / 2 + PICTURE_ROW / 2
        for index, (kind, key, label) in enumerate(column):
            y = round(top + index * PICTURE_ROW, 1)
            places[key] = (x, y)
            name = short(label) if kind == "knowledge" else short(label.rsplit("/", 1)[-1])
            nodes.append(
                {
                    "kind": kind, "x": x, "y": y, "label": name, "title": label,
                    "box_x": x - HALF_BOX, "box_y": round(y - 14, 1), "text_y": round(y + 4.5, 1),
                    # A file the run created is drawn only after it.
                    "created": kind != "knowledge" and key not in existed,
                }
            )

    def curve(start, end):
        (x1, y1), (x2, y2) = places[start], places[end]
        if x1 == x2:
            bulge = x1 + HALF_BOX + 35
            return (
                f"M{x1 + HALF_BOX},{y1} C{bulge},{y1} {bulge},{y2} {x2 + HALF_BOX},{y2}"
            )
        left, right = (
            (x1 + HALF_BOX, x2 - HALF_BOX) if x1 < x2 else (x1 - HALF_BOX, x2 + HALF_BOX)
        )
        middle = (left + right) / 2
        return f"M{left},{y1} C{middle},{y1} {middle},{y2} {right},{y2}"

    def state(in_before, in_after):
        return "same" if in_before and in_after else ("new" if in_after else "gone")

    shown = set(changed) | set(dependencies)
    edges = []
    for source, target, kind in sorted(was | now):
        if source in places and target in places and source in changed:
            edges.append(
                {
                    "d": curve(source, target),
                    "kind": "code",
                    "label": kind,
                    "state": state((source, target, kind) in was, (source, target, kind) in now),
                }
            )
    for node in chosen:
        paths = linked_paths[node["id"]]
        for path in sorted((paths["before"] | paths["after"]) & shown):
            edges.append(
                {
                    "d": curve(node["id"], path),
                    "kind": "link",
                    "label": "shared identifier",
                    "state": state(path in paths["before"], path in paths["after"]),
                }
            )
    return {
        "width": PICTURE_WIDTH,
        "height": height,
        "nodes": nodes,
        "before": [edge for edge in edges if edge["state"] != "new"],
        "after": [edge for edge in edges if edge["state"] != "gone"],
        "new_count": sum(1 for edge in edges if edge["state"] == "new"),
        "gone_count": sum(1 for edge in edges if edge["state"] == "gone"),
    }


def before_after(run):
    """What this application knew before the run, and what it knows now.

    Read from the record, never assumed: the snapshot the run reasoned about
    against the newest one of the same repository, the knowledge version it read
    against the newest built, and - per changed file - the knowledge items that
    are linked to it now and were not before. None until the change has left
    this platform; `pending` while the re-index it asked for is still running.
    """
    from .code_knowledge import links
    from .models import GraphRevision

    if not run.delivered or run.code_snapshot_id is None:
        return None
    before = run.code_snapshot
    after = before.repository.snapshots.first()
    revisions = GraphRevision.objects.filter(application_id=run.application_id)
    read = revisions.filter(number=run.graph_version).first() if run.graph_version else None
    latest = revisions.first()
    knowledge = {"before": read, "after": latest if latest and latest != read else None}
    if after is None or after.pk == before.pk:
        return {
            "pending": "code" in (run.refresh_scope or []),
            "before": before,
            "knowledge": knowledge,
        }

    old = dict(before.files.values_list("path", "digest"))
    new = dict(after.files.values_list("path", "digest"))
    touched = sorted(path for path, digest in new.items() if old.get(path) != digest)

    def edges(snapshot):
        return set(
            snapshot.relationships.values_list("source__path", "target__path", "kind")
        )

    was, now = edges(before), edges(after)
    connections = []
    if read:
        earlier = dict(before.files.filter(path__in=touched).values_list("path", "pk"))
        for file in after.files.filter(path__in=touched).only("pk", "path"):
            found, _ = links(read, after, file_id=file.pk)
            known = set()
            if file.path in earlier:
                earlier_links, _ = links(read, before, file_id=earlier[file.path])
                known = {item["node_id"] for item in earlier_links}
            gained = {}
            for item in found:
                if item["node_id"] not in known:
                    gained.setdefault(item["node_id"], item)
            if gained:
                # Identifiers first - T-03, BR-05, C-09 - because they are what a
                # reader recognises; long passages that merely mention one follow.
                # One row per label, since a table repeats the same cell.
                ranked = sorted(
                    gained.values(),
                    key=lambda item: (len(item["node_label"]) > 24, item["node_label"]),
                )
                shown = list({item["node_label"]: item for item in reversed(ranked)}.values())
                shown.reverse()
                connections.append(
                    {
                        "path": file.path,
                        "file_id": file.pk,
                        "count": len(gained),
                        "items": shown[:8],
                        "more": max(len(shown) - 8, 0),
                    }
                )
    picture = (
        change_picture(read, before, after, touched, was, now, connections) if read else None
    )
    if picture:
        newest = knowledge["after"] or read
        picture["sides"] = [
            {
                "key": "before",
                "title": "Before",
                "detail": f"code snapshot v{before.number} · knowledge graph v{read.number}",
                "edges": picture["before"],
            },
            {
                "key": "after",
                "title": "After",
                "detail": f"code snapshot v{after.number} · knowledge graph v{newest.number}",
                "edges": picture["after"],
            },
        ]
    return {
        "pending": False,
        "picture": picture,
        "before": before,
        "after": after,
        "knowledge": knowledge,
        "changed": [path for path in touched if path in old],
        "added": [path for path in touched if path not in old],
        "removed": sorted(set(old) - set(new)),
        "new_edges": sorted(now - was)[:12],
        "gone_edges": sorted(was - now)[:12],
        "connections": connections,
    }


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
    if run.checks and all(entry.get("conclusion") for entry in run.checks):
        # Settled. Touch the timestamp so it falls to the back of the queue
        # rather than being asked about forever.
        FactoryRun.objects.filter(pk=run.pk).update(checks_read_at=timezone.now())
        return False
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
            notes.append("No repository is registered in Code Graph.")
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
