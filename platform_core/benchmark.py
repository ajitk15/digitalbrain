"""Known questions, scored against a graph version: is the right source cited?

Every later retrieval change - BM25, an ontology, merged names, embeddings,
more hops - claims to find better evidence. This is what checks the claim. An
owner writes questions with the source each should cite; every saved graph
version is scored against them, so "is v4 better than v3?" has an answer
before anyone publishes v4.

Retrieval only. The questions go through `graph_ai.citations_from`, the same
ranking and citation verification an answer uses, and stop there: no model is
called and nothing is charged. A draft can be measured because measuring is
not answering - which graph may answer is still `graph_snapshot`'s decision,
and it still refuses drafts.
"""

import csv
import io
import json
import logging
import uuid
import zipfile
from pathlib import Path

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import BenchmarkQuestion, BenchmarkRun, GraphRevision, KnowledgeEntry
from .services import audit

logger = logging.getLogger(__name__)

#: Enough to cover an application's important questions; a benchmark that
#: takes minutes to read is a benchmark nobody reads.
MAX_QUESTIONS = 100
#: An answer leans hardest on its first few citations.
TOP = 3


class BenchmarkQuestionForm(forms.Form):
    question = forms.CharField(
        max_length=500,
        widget=forms.TextInput(
            attrs={"placeholder": "Is consent checked before a partner export?"}
        ),
    )
    expected = forms.CharField(
        max_length=200,
        label="Should cite",
        help_text="Text in the expected source's title or link, such as a ticket key "
        "(KAN-4) or a document name (deployment-runbook).",
        widget=forms.TextInput(attrs={"placeholder": "KAN-4"}),
    )

    def clean_expected(self):
        value = self.cleaned_data["expected"].strip()
        if len(value) < 3:
            raise forms.ValidationError("Use at least three characters, so it matches one source.")
        return value


#: A question file is a few kilobytes; this leaves room for a spreadsheet's
#: zipped XML and no more.
MAX_IMPORT_BYTES = 1024 * 1024
#: What an .xlsx may unzip to. openpyxl reads its shared strings whole, so a
#: small zip of repetitive XML must not become a large read in the web process.
MAX_UNZIPPED_BYTES = 10 * 1024 * 1024
#: Column headings a file may use, folded to lower case, and the field each fills.
HEADINGS = {"question": "question", "expected": "expected", "should cite": "expected"}
FORMATS = (".json", ".csv", ".xlsx")


class BenchmarkImportForm(forms.Form):
    file = forms.FileField(
        label="Questions file",
        help_text="JSON (a list of {\"question\": ..., \"expected\": ...}), CSV or Excel "
        "(.xlsx) with Question and Should cite columns. Questions already here are skipped.",
        widget=forms.ClearableFileInput(attrs={"accept": ",".join(FORMATS)}),
    )


def read_questions(name, data):
    """The questions in an uploaded file, as (where, item) pairs.

    `where` names the row or item, so a refusal can say which one. The same
    columns in every format: question, and expected or "Should cite" - what the
    page's own table calls it, so a sheet copied from it reads back.
    """
    if len(data) > MAX_IMPORT_BYTES:
        raise ValidationError(f"Keep the file under {MAX_IMPORT_BYTES // 1024} KB.")
    suffix = Path(name or "").suffix.lower()
    if suffix == ".json":
        try:
            items = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValidationError(f"That is not JSON: {error}") from None
        if not isinstance(items, list):
            raise ValidationError("The file must hold a JSON list.")
        return [(f"Item {number}", item) for number, item in enumerate(items, 1)]
    if suffix == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationError("Save the CSV as UTF-8.") from None
        return tabulated(csv.reader(io.StringIO(text)))
    if suffix == ".xlsx":
        return tabulated(spreadsheet_rows(data))
    raise ValidationError("Upload a .json, .csv or .xlsx file.")


def spreadsheet_rows(data):
    """The first sheet's rows as values, read without formulas or macros."""
    import openpyxl

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unzipped = sum(member.file_size for member in archive.infolist())
    except zipfile.BadZipFile:
        raise ValidationError("That is not an Excel .xlsx file.") from None
    if unzipped > MAX_UNZIPPED_BYTES:
        raise ValidationError("That workbook is too large for a list of questions.")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ValidationError("That Excel file could not be read.") from None
    try:
        yield from workbook.worksheets[0].iter_rows(values_only=True)
    finally:
        workbook.close()


def tabulated(rows):
    """Rows under a heading row, as (where, item); blank rows are skipped."""
    columns = None
    found = []
    for number, row in enumerate(rows, 1):
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        # A sheet's row ends at its last filled cell, so pad it to the heading's width.
        if columns:
            cells += [""] * (max(columns) + 1 - len(cells))
        if not any(cells):
            continue
        if columns is None:
            columns = {
                index: HEADINGS[cell.casefold()]
                for index, cell in enumerate(cells)
                if cell.casefold() in HEADINGS
            }
            if sorted(set(columns.values())) != ["expected", "question"]:
                raise ValidationError(
                    "The first row must name the columns: Question and Should cite "
                    "(or expected)."
                )
            continue
        found.append((f"Row {number}", {field: cells[index] for index, field in columns.items()}))
        # More than an application may hold is refused anyway; stop reading.
        if len(found) > MAX_QUESTIONS:
            break
    if columns is None:
        raise ValidationError("The file is empty.")
    return found


def load_questions(app, rows, user=None):
    """Add questions from (where, item) pairs, all or none.

    A question already held - the same words - is skipped, so a file can be
    loaded again after it grows. Returns (added, skipped).
    """
    existing = set(
        BenchmarkQuestion.objects.filter(application=app).values_list("question", flat=True)
    )
    held = len(existing)
    labels = {
        name: str(field.label or name).capitalize()
        for name, field in BenchmarkQuestionForm.base_fields.items()
    }
    new, skipped, errors = [], 0, []
    for where, item in rows:
        form = BenchmarkQuestionForm(item if isinstance(item, dict) else {})
        if not form.is_valid():
            errors.extend(
                f"{where}: {labels.get(field, field)} - {message}"
                for field, messages_ in form.errors.items()
                for message in messages_
            )
            continue
        if form.cleaned_data["question"] in existing:
            skipped += 1
            continue
        existing.add(form.cleaned_data["question"])
        new.append(form.cleaned_data)
    if errors:
        more = len(errors) - 5
        raise ValidationError(errors[:5] + ([f"... and {more} more."] if more > 0 else []))
    if held + len(new) > MAX_QUESTIONS:
        raise ValidationError(
            f"An application can hold {MAX_QUESTIONS} questions. This one holds {held}, "
            f"and the file adds {len(new)}."
        )
    BenchmarkQuestion.objects.bulk_create(
        BenchmarkQuestion(application=app, created_by=user, **question) for question in new
    )
    return len(new), skipped


def matches(entry, expected):
    """Whether a cited source is the one a question expects."""
    needle = expected.casefold()
    return needle in entry.title.casefold() or needle in (entry.source or "").casefold()


def score(app, revision, questions, reranker=None, data=None):
    """Each question's outcome against one version, in the order asked.

    The rank counts distinct sources, not citations: three relationships from
    one ticket are one source an answer was shown, and counting them three
    times would let a source crowd its own rank.

    `reranker` is the person a reranked run is charged to. Only the console
    passes one (`manage.py benchmark --rerank --as`): it is the one benchmark
    that spends, and it exists so reranking is switched on because it was
    measured to help. `data`, when given, is scored in place of the version's
    own - `graph_trend.without_ai` of it, for the same version without AI.
    """
    from . import rerank
    from .embeddings import question_vector
    from .graph_ai import citations_from, prepare
    from .permissions import EVERYONE

    data = revision.data if data is None else data
    # Built once for every question: a version's relationships are the same for each.
    ready = prepare(data)
    # Stored question vectors only - no user, so nothing is ever requested and a
    # run stays free. An AI graph run embeds the questions for this.
    vectors = [question_vector(None, app.pk, question.question) for question in questions]
    cited = [
        citations_from(
            app.pk,
            data,
            revision.number,
            question.question,
            ready,
            vector=vector,
            model=model,
            limit=rerank.CANDIDATES if reranker else 8,
            # A measurement of the whole corpus, shown to the people who
            # maintain it; it answers nobody, so no one's permissions apply.
            viewer=EVERYONE,
        )
        for question, (vector, model) in zip(questions, vectors, strict=True)
    ]
    if reranker is not None:
        cited = [
            rerank.rerank(reranker, app.pk, question.question, citations, 8)
            for question, citations in zip(questions, cited, strict=True)
        ]
    ids = {citation["id"] for citations in cited for citation in citations}
    entries = {
        str(entry.pk): entry
        for entry in KnowledgeEntry.objects.filter(application=app, pk__in=ids).only(
            "id", "title", "source"
        )
    }
    results = []
    for question, citations, (vector, _) in zip(questions, cited, vectors, strict=True):
        sources = list(dict.fromkeys(citation["id"] for citation in citations))
        rank = next(
            (
                position
                for position, source_id in enumerate(sources, 1)
                if source_id in entries and matches(entries[source_id], question.expected)
            ),
            None,
        )
        results.append(
            {
                "question": question.question,
                "expected": question.expected,
                "rank": rank,
                "cited": [entries[s].title for s in sources if s in entries],
                # Whether meaning took part, so a score can say what produced it.
                "vector": vector is not None,
                "reranked": reranker is not None,
            }
        )
    return results


def run(app, revision, user=None, trigger="saved", reranker=None):
    """Score a version against this application's questions; None if it has none."""
    questions = list(BenchmarkQuestion.objects.filter(application=app)[:MAX_QUESTIONS])
    if not questions:
        return None
    from .graph_trend import score_structure

    results = score(app, revision, questions, reranker=reranker)
    ranks = [result["rank"] for result in results]
    structure = None if reranker else score_structure(app, revision, questions)
    return BenchmarkRun.objects.create(
        application=app,
        revision=revision,
        questions=len(results),
        found=sum(rank is not None for rank in ranks),
        top_three=sum(rank is not None and rank <= TOP for rank in ranks),
        reciprocal_rank=round(sum(1 / rank for rank in ranks if rank) / len(ranks), 3),
        results=results,
        structure=structure,
        trigger=trigger,
        created_by=user,
    )


def run_after_save(revision_pk):
    """The automatic run for a freshly saved version, and its measures. Never fails the save.

    A benchmark is a report about a version, not a condition of having one: a
    question that trips an error here must not cost the application its graph.
    """
    from .graph_trend import measure

    revision = GraphRevision.objects.filter(pk=revision_pk).select_related("application").first()
    if revision is None:
        return
    try:
        run(revision.application, revision)
    except Exception:
        logger.exception("benchmark_failed", extra={"event": "benchmark_failed"})
    try:
        measure(revision)
    except Exception:
        logger.exception("graph_measures_failed", extra={"event": "graph_measures_failed"})
    # After the benchmark and the measures, which it compares: whether this
    # version may replace the one that answers (`release.check`), for the strip
    # and the Versions table. Publishing checks again.
    from .release import check_after_save

    check_after_save(revision.pk)


def latest_runs(app, revisions):
    """The newest run for each of these versions, keyed by revision pk."""
    latest = {}
    for item in BenchmarkRun.objects.filter(application=app, revision__in=revisions).only(
        "revision_id", "questions", "found", "top_three", "reciprocal_rank", "created_at"
    ):
        latest.setdefault(item.revision_id, item)
    return latest


def rows(questions, selected):
    """One row per question, with what the selected run made of it.

    The questions and a run's results used to be two tables listing the same
    questions twice. A run stores its questions as text, so a question is
    matched by its words and expected source; one added since the run reads
    "Not scored yet", and one removed since still shows, because it is in the
    run's count.
    """
    results = {
        (result["question"], result["expected"]): result
        for result in (selected.results if selected else [])
    }
    merged = []
    for question in questions:
        result = results.pop((question.question, question.expected), None)
        merged.append({"question": question, "result": result})
    merged.extend({"question": None, "result": result} for result in results.values())
    return merged


@login_required
@require_http_methods(["GET", "POST"])
def benchmark_view(request, pk):
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge")
    form = BenchmarkQuestionForm()
    import_form = BenchmarkImportForm()
    if request.method == "GET" and request.GET.get("download") == "json":
        # The import's own format, so one instance's questions load into another.
        questions = BenchmarkQuestion.objects.filter(application=app).order_by("created_at")
        response = HttpResponse(
            json.dumps(
                [{"question": item.question, "expected": item.expected} for item in questions],
                indent=2,
                ensure_ascii=False,
            ),
            content_type="application/json; charset=utf-8",
        )
        response["Content-Disposition"] = 'attachment; filename="benchmark-questions.json"'
        return response
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        organization = app.product.portfolio.organization
        action = request.POST.get("action")
        if action == "import":
            import_form = BenchmarkImportForm(request.POST, request.FILES)
            if import_form.is_valid():
                upload = import_form.cleaned_data["file"]
                try:
                    if upload.size > MAX_IMPORT_BYTES:
                        raise ValidationError(
                            f"Keep the file under {MAX_IMPORT_BYTES // 1024} KB."
                        )
                    with transaction.atomic():
                        added, skipped = load_questions(
                            app, read_questions(upload.name, upload.read()), request.user
                        )
                except ValidationError as error:
                    import_form.add_error("file", error)
                else:
                    audit(
                        request.user,
                        "benchmark.questions_imported",
                        app.pk,
                        organization,
                        {"added": added, "skipped": skipped},
                    )
                    messages.success(
                        request,
                        f"Added {added} question(s)"
                        + (f"; {skipped} already here were skipped" if skipped else "")
                        + ". Run the benchmark to score them.",
                    )
                    return redirect("graph-benchmark", pk=pk)
        elif action == "add":
            form = BenchmarkQuestionForm(request.POST)
            if BenchmarkQuestion.objects.filter(application=app).count() >= MAX_QUESTIONS:
                form.add_error(None, f"An application can hold {MAX_QUESTIONS} questions.")
            if form.is_valid():
                question = BenchmarkQuestion.objects.create(
                    application=app, created_by=request.user, **form.cleaned_data
                )
                audit(request.user, "benchmark.question_added", question.pk, organization)
                messages.success(request, "Question added. Run the benchmark to score it.")
                return redirect("graph-benchmark", pk=pk)
        elif action == "delete":
            try:
                question_id = uuid.UUID(request.POST.get("question", ""))
            except ValueError:
                return HttpResponse("Invalid question.", status=400)
            deleted, _ = BenchmarkQuestion.objects.filter(application=app, pk=question_id).delete()
            if deleted:
                audit(request.user, "benchmark.question_removed", question_id, organization)
                messages.success(request, "Question removed.")
            return redirect("graph-benchmark", pk=pk)
        elif action == "run":
            number = request.POST.get("version", "")
            if not number.isdecimal() or len(number) > 9:
                return HttpResponse("Invalid graph version.", status=400)
            revision = get_object_or_404(GraphRevision, application=app, number=int(number))
            with transaction.atomic():
                result = run(app, revision, request.user, trigger="button")
            if result is None:
                messages.info(request, "Add a question first - there is nothing to score.")
            else:
                messages.success(
                    request,
                    f"Version {revision.number}: {result.found} of {result.questions} "
                    "questions cited the expected source.",
                )
            return redirect(f"{request.path}?run={result.pk}" if result else request.path)
        else:
            return HttpResponse("Unknown benchmark action.", status=400)

    revisions = list(GraphRevision.objects.filter(application=app).defer("data")[:10])
    latest = latest_runs(app, revisions)
    for revision in revisions:
        revision.benchmark = latest.get(revision.pk)
    selected = None
    run_id = request.GET.get("run", "")
    if run_id:
        try:
            selected = BenchmarkRun.objects.filter(
                application=app, pk=uuid.UUID(run_id)
            ).select_related("revision").first()
        except ValueError:
            return HttpResponse("Invalid run.", status=400)
    if selected is None:
        selected = (
            BenchmarkRun.objects.filter(application=app).select_related("revision").first()
        )
    questions = list(BenchmarkQuestion.objects.filter(application=app))
    return render(
        request,
        "graph_benchmark.html",
        {
            "application": app,
            "grant": grant,
            "knowledge_view": "benchmark",
            "can_edit": grant.role in {"owner", "contributor"},
            "form": form,
            "import_form": import_form,
            "questions": questions,
            "rows": rows(questions, selected),
            "max_questions": MAX_QUESTIONS,
            "revisions": revisions,
            "selected": selected,
            "top": TOP,
        },
    )
