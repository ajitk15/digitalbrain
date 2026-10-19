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

import logging
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
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
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        organization = app.product.portfolio.organization
        action = request.POST.get("action")
        if action == "add":
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
            "questions": questions,
            "rows": rows(questions, selected),
            "max_questions": MAX_QUESTIONS,
            "revisions": revisions,
            "selected": selected,
            "top": TOP,
        },
    )
