"""Application knowledge, evidence retrieval and immutable approval workflows."""

import hashlib
import re
import threading
from collections import OrderedDict
from datetime import timedelta

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .agent_runtime.runtime import HISTORY_TURNS, HistoryTurn
from .models import CHAT_MODES, ChatConversation, ChatMessage, KnowledgeEntry
from .policy import application_for
from .services import audit, feature_enabled


def access(user, pk, feature, write=False):
    app, grant = application_for(user, pk)
    if not feature_enabled(feature, app):
        raise PermissionDenied("This feature is disabled.")
    if write and grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    return app, grant


class QuestionForm(forms.Form):
    question = forms.CharField(
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "Ask a question about your documents…"}
        ),
        label="Ask your application knowledge",
    )


@transaction.atomic
def add_knowledge(user, app_id, title, content, source="", document=None):
    from .document_ai import cached_type
    from .masking import prepare
    from .sensitivity import RESTRICTED, STANDARD, scan
    from .source_quality import run as check_source

    app, _ = access(user, app_id, "knowledge", write=True)
    # Before anything is computed from it: the stored text is the masked text, so
    # the digest, quotes and every reader downstream only ever see placeholders.
    title, content, masked = prepare(app.pk, title, content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    # A restriction follows the record: the next import of the same ticket must
    # not quietly make it usable again.
    inherited = bool(source) and (
        KnowledgeEntry.objects.filter(
            application=app, source=source, sensitivity=RESTRICTED
        ).exists()
    )
    entry = KnowledgeEntry(
        application=app,
        author=user,
        title=title,
        content=content,
        source=source,
        document=document,
        digest=digest,
        personal_data=scan(f"{title}\n{content}"),
        masked=masked,
        # Documents only: a typed note or an imported ticket is not a document,
        # and a ticket has its own score (`ticket_quality`). A type a model
        # already named for this exact text is kept; nothing is asked here.
        quality=check_source(
            content, document.conversion_report, title, ai_type=cached_type(app.pk, digest)
        ) if document else {},
        sensitivity=RESTRICTED if inherited else STANDARD,
    )
    entry.full_clean()
    entry.save()
    audit(user, "knowledge.created", entry.pk, app.product.portfolio.organization)
    return entry


#: Passages are windows this long, starting this far apart, so a sentence on a
#: boundary is whole in one of them. Search embeddings index the same windows.
PASSAGE_LENGTH = 1400
PASSAGE_STEP = 1200
#: Sources whose passages' terms are kept, least recently used dropped first.
#: Splitting and stemming every passage on every question was four fifths of a
#: keyword search's time, for text that had not changed since the last one.
PASSAGE_CACHE = 2000
_passage_terms = OrderedDict()
_passage_lock = threading.Lock()


def passage_counts(entry):
    """Each passage's (term counts, length), for BM25, cached per source text.

    Keyed by the source's digest, title and length together, so an edited
    source is split again rather than ranked by what it used to say.
    """
    from collections import Counter

    from .ranking import terms

    key = (entry.pk, entry.digest, entry.title, len(entry.content))
    with _passage_lock:
        found = _passage_terms.get(key)
        if found is not None:
            _passage_terms.move_to_end(key)
            return found
    found = []
    for offset in range(0, len(entry.content), PASSAGE_STEP):
        split = terms(f"{entry.title} {entry.content[offset : offset + PASSAGE_LENGTH]}")
        found.append((Counter(split), len(split)))
    with _passage_lock:
        _passage_terms[key] = found
        while len(_passage_terms) > PASSAGE_CACHE:
            _passage_terms.popitem(last=False)
    return found


#: Sources keyword search ranks per question.
CANDIDATE_SOURCES = 100
#: PostgreSQL's full text: a column generated from the first 200,000 characters
#: of title and content (migration 0088), so a question never tokenizes a source
#: again. A tsvector holds at most 1 MB, and a source past that is a log, not prose.
FULL_TEXT = '"platform_core_knowledgeentry"."search_vector"'


def full_text_query(question):
    """A PostgreSQL tsquery for a question: each word as a prefix, any may match.

    Built from `ranking.words`, which are letters, digits and nothing else, so
    nothing in a question can become tsquery syntax.
    """
    from .ranking import term, words

    needles = []
    for word in list(dict.fromkeys(words(question)))[:20]:
        needle = term(word) if word.startswith(term(word)) else word
        if needle.isalnum():
            needles.append(f"{needle}:*")
    return " | ".join(needles)


def candidate_sources(app, question):
    """The sources keyword search ranks: the best 100 by full text on PostgreSQL.

    On PostgreSQL the GIN index chooses them, ordered by its own rank, so a
    library of thousands still offers the hundred most likely - the cap used
    to be the first hundred that matched any word. SQLite keeps the substring
    scan, which is what local development and the tests run.
    """
    from django.db import connection

    from .ranking import term, words

    entries = KnowledgeEntry.objects.filter(application=app).answerable()
    if connection.vendor == "postgresql":
        query = full_text_query(question)
        if not query:
            return []
        return list(
            entries.extra(
                select={"text_rank": f"ts_rank({FULL_TEXT}, to_tsquery('english', %s))"},
                select_params=[query],
                where=[f"{FULL_TEXT} @@ to_tsquery('english', %s)"],
                params=[query],
                order_by=["-text_rank"],
            )[:CANDIDATE_SOURCES]
        )
    # The database narrows by the term where it is a prefix of the word, so
    # "checked" also finds "checks" and "requirements" finds "require";
    # ranking compares terms throughout.
    condition = Q()
    for word in list(dict.fromkeys(words(question)))[:20]:
        needle = term(word) if word.startswith(term(word)) else word
        condition |= Q(content__icontains=needle) | Q(title__icontains=needle)
    return list(entries.filter(condition)[:CANDIDATE_SOURCES])


def chunk_counts(chunk):
    """A stored passage's (term counts, length), cached like `passage_counts`."""
    from collections import Counter

    from .ranking import terms

    key = ("chunk", chunk.digest, chunk.heading)
    with _passage_lock:
        found = _passage_terms.get(key)
        if found is not None:
            _passage_terms.move_to_end(key)
            return found
    split = terms(f"{chunk.heading} {chunk.text}")
    found = (Counter(split), len(split))
    with _passage_lock:
        _passage_terms[key] = found
        while len(_passage_terms) > PASSAGE_CACHE:
            _passage_terms.popitem(last=False)
    return found


def retrieve(app, question, user=None, limit=5, candidates=None):
    """Up to five passages: word matches by BM25, fused with the nearest by meaning.

    Deterministic and lexical unless the application switched on search
    embeddings, and then the question's vector only adds candidates: each one
    is re-read from its source, and skipped unless the passage still reads
    exactly as it did when it was embedded. `user` is who a new question
    vector is charged to; without one only a stored vector is used.

    `candidates` is where word matches come from: "sources" cuts the best
    hundred sources into passages here, "chunks" lets the database choose
    stored passages (`chunks.py`). The deployment's `retrieval_candidates`
    decides when it is not given; everything after the candidates is the same.
    """
    from django.conf import settings

    from .embeddings import digest as text_digest
    from .embeddings import fuse, nearest, passage_matrix, passage_text, question_vector
    from .permissions import only_permitted, permitted
    from .ranking import BM25, query_terms, spread

    query = query_terms(question)
    vector, model = question_vector(user, app.pk, question)
    if not query and vector is None:
        return []
    candidates = candidates or settings.RETRIEVAL_CANDIDATES
    if candidates == "auto":
        from .chunks import complete

        candidates = "chunks" if complete(app) else "sources"
    passages, texts, counts = [], [], []
    if query and candidates == "chunks":
        from .chunks import candidates as chunk_candidates

        for chunk in chunk_candidates(app, question):
            if permitted(chunk.entry, user):
                passages.append((chunk.entry, chunk.offset))
                texts.append(chunk.text)
                counts.append(chunk_counts(chunk))
    elif query:
        entries = only_permitted(candidate_sources(app, question), user)
        for entry in entries:
            split = passage_counts(entry)
            for offset in range(0, len(entry.content), PASSAGE_STEP):
                passages.append((entry, offset))
                texts.append(entry.content[offset : offset + PASSAGE_LENGTH])
                counts.append(split[offset // PASSAGE_STEP])
    by_meaning = []
    if vector is not None:
        rows, matrix = passage_matrix(app.pk, model)
        hits = [rows[row] for row in nearest(matrix, vector)]
        live = {
            entry.pk: entry
            for entry in KnowledgeEntry.objects.filter(
                application=app, pk__in={entry_id for entry_id, _, _ in hits}
            ).answerable()
        }
        known = {(entry.pk, offset): position for position, (entry, offset) in enumerate(passages)}
        for entry_id, offset, key in hits:
            entry = live.get(entry_id)
            if (
                entry is None
                or not permitted(entry, user)
                or text_digest(passage_text(entry, offset)) != key
            ):
                continue
            if (entry.pk, offset) not in known:
                known[(entry.pk, offset)] = len(passages)
                passages.append((entry, offset))
                texts.append(entry.content[offset : offset + PASSAGE_LENGTH])
                counts.append(passage_counts(entry)[offset // PASSAGE_STEP])
            by_meaning.append(known[(entry.pk, offset)])
    # The same ranking the graph uses - passages holding more of the question
    # first, then BM25 - and the same spread: five passages from one document
    # are one source, so each document's best passage comes before its second.
    index = BM25(counts=counts)
    scored = index.rank_covering(query) if query else []
    scores = {position: score for score, position in scored}
    order = [position for _, position in scored]
    if by_meaning:
        order = fuse(order, by_meaning)
    ranked = [
        (scores.get(position, 0.0), passages[position][0], texts[position]) for position in order
    ]
    return spread(ranked, key=lambda item: item[1].pk, limit=limit)


def readable_evidence(citations):
    if not citations:
        return (
            "No matching evidence was found in this application's knowledge. "
            "Try naming a document, system or topic, or add a relevant source in Knowledge."
        )
    parts = ["Here's what I found in your application's sources:"]
    for index, citation in enumerate(citations, 1):
        # Extractive fallback: preserve source wording rather than invent a synthesis.
        excerpt = citation["excerpt"].strip()
        if len(excerpt) > 650:
            excerpt = excerpt[:650].rsplit(" ", 1)[0] + "…"
        parts.append(f"**{citation['title']} [{index}]**\n\n{excerpt}")
    parts.append("These are source excerpts. Choose AI answer for a conversational explanation.")
    return "\n\n".join(parts)


def conversation_context(conversation, app, user, before=None):
    """Recent exchanges, as provider-neutral HistoryTurn values.

    Returns plain values rather than model instances so a worker thread can carry
    history without holding an ORM object bound to a request.

    `before` excludes the tail a regenerate or edit is about to replace. Those
    messages still exist at this point - they are removed only once a replacement
    answer exists - so they have to be filtered out here or the model would be
    shown the very answer it is being asked to redo.
    """
    if not conversation:
        return []
    history = conversation.messages.filter(application=app, user=user)
    if before is not None:
        history = history.filter(sequence__lt=before)
    answers = list(
        history.filter(role="assistant", status="complete").order_by("-sequence")[:HISTORY_TURNS]
    )
    if not answers:
        return []
    questions = {
        message.sequence: message.body
        for message in history.filter(
            role="user", sequence__in=[answer.sequence - 1 for answer in answers]
        )
    }
    from .permissions import permitted

    # Never resend excerpts/answers backed by a removed or changed source. Look up
    # only the cited sources rather than every source in the application.
    cited = {c.get("id") for answer in answers for c in answer.citations}
    active = (
        {
            str(e.pk): e.digest
            for e in KnowledgeEntry.objects.filter(
                application=app, pk__in=[c for c in cited if c]
            ).answerable().only("id", "digest", "access_principals")
            # Nor anything its source system no longer lets this person open.
            if permitted(e, user)
        }
        if cited
        else {}
    )
    return [
        HistoryTurn(question=questions[answer.sequence - 1], answer=answer.body)
        for answer in reversed(answers)
        if answer.sequence - 1 in questions
        and all(active.get(c.get("id")) == c.get("digest") for c in answer.citations)
    ]


def lock_conversation(conversation):
    """Take the row lock the sequence allocator depends on.

    next_sequence reads the highest sequence and adds one, which is only safe
    while nothing else can do the same. The lock was documented but never
    actually taken: two concurrent sends could read the same value and both write
    it, violating the unique constraint after the provider had already been paid.
    SQLite serialises writers anyway, which is why the tests never showed it;
    PostgreSQL does not.
    """
    return ChatConversation.objects.select_for_update().filter(pk=conversation.pk).first()


def next_sequence(conversation):
    """Allocate the next message slot.

    Must be called inside a transaction that already holds the conversation row
    through lock_conversation, or two concurrent sends can claim one sequence.
    """
    last = conversation.messages.order_by("-sequence").values_list("sequence", flat=True).first()
    return 0 if last is None else last + 1


def record_exchange(
    app, user, conversation, question, answer, citations, mode, status="complete", submission=""
):
    """Persist one question and its answer as two ordered messages."""
    sequence = next_sequence(conversation)
    ask = ChatMessage.objects.create(
        application=app,
        user=user,
        conversation=conversation,
        role="user",
        status="complete",
        body=question,
        mode=mode,
        sequence=sequence,
        submission=submission,
    )
    reply = ChatMessage.objects.create(
        application=app,
        user=user,
        conversation=conversation,
        role="assistant",
        status=status,
        body=answer,
        citations=citations,
        mode=mode,
        sequence=sequence + 1,
        finished_at=timezone.now(),
    )
    return ask, reply


def graph_version_choices(app_id):
    """Saved graph versions a conversation may be pinned to."""
    from .graph_ai import available_graph_versions

    return available_graph_versions(app_id)


def new_graph_version(request, app_id, graph_ai_enabled):
    """The graph version a conversation is pinned to at the moment it starts.

    A conversation freezes on the version it began with. Publishing a new graph
    afterwards must not change what an existing conversation answers from, or a
    thread silently becomes a mixture of two graphs and its earlier answers stop
    being reproducible. So when the user does not choose a version we pin the one
    published right now rather than leaving it open.
    """
    if not graph_ai_enabled:
        return None
    from .graph_ai import available_graph_versions

    raw = request.POST.get("graph_version", "")
    try:
        chosen = int(raw)
    except (TypeError, ValueError):
        chosen = None
    if chosen is not None and chosen in available_graph_versions(app_id):
        return chosen
    return published_graph_version(app_id)


def retention_days(app):
    """Days of chat history this application keeps; 0 means indefinitely."""
    from .models import ChatRetention

    record = ChatRetention.objects.filter(application=app).first()
    return record.days if record else ChatRetention.DEFAULT_DAYS


def purge_expired_conversations(app=None):
    """Delete conversations untouched for longer than the retention window.

    Keyed on updated_at, not created_at: a thread someone is still using is not
    stale. A window of 0 disables the purge for that application.
    """
    from .models import Application, ChatConversation, ChatMessage

    applications = [app] if app is not None else list(Application.objects.all())
    removed = 0
    for target in applications:
        days = retention_days(target)
        if not days:
            continue
        cutoff = timezone.now() - timedelta(days=days)
        stale = ChatConversation.objects.filter(application=target, updated_at__lt=cutoff)
        if not stale.exists():
            continue
        ChatMessage.objects.filter(conversation__in=stale).delete()
        removed += stale.delete()[0]
    return removed


def clear_conversations(user, app):
    """Delete every conversation this user holds in this application.

    Scoped to the one user: a conversation is private to whoever started it, so
    "clear all" must never reach another member's history.
    """
    from .models import ChatConversation, ChatMessage

    conversations = ChatConversation.objects.filter(application=app, user=user)
    count = conversations.count()
    ChatMessage.objects.filter(conversation__in=conversations).delete()
    conversations.delete()
    if count:
        audit(
            user,
            "chat.cleared",
            app.pk,
            app.product.portfolio.organization,
            details={"conversations": count},
        )
    return count


def published_graph_version(app_id):
    """The version graph answers use unless a conversation pins another."""
    from .graphs import published_revision

    revision = published_revision(app_id)
    return revision.number if revision else None


def credential_available(config, app):
    """Whether a run could authenticate, by mounted secret or development host login."""
    from .agent_runtime.credentials import host_login_enabled
    from .secrets import source_of

    if source_of(app, config.provider):
        return True
    return config.provider == "claude" and host_login_enabled()


def mode_options(ai_enabled, graph_ai_enabled):
    """Every answer mode, each marked available or not.

    Unavailable modes are still listed rather than hidden. A capability that simply
    vanishes teaches nobody it exists; one that says "setup required" points at the
    thing to configure.
    """
    reasons = {
        "ai": "" if ai_enabled else "setup required",
        "graph": "" if graph_ai_enabled else "setup required",
        "search": "",
    }
    return [
        {
            "value": value,
            "label": label,
            "note": reasons.get(value, ""),
            "available": not reasons.get(value),
        }
        for value, label in CHAT_MODES
    ]


def available_modes(graph_ai_enabled, ai_enabled=True):
    """Modes a conversation may be switched to. Search never needs configuration.

    Uses the same availability `mode_options` shows: a mode the selector marks
    "setup required" is not one a conversation can be put in.
    """
    return [
        (value, label)
        for value, label in CHAT_MODES
        if (value != "graph" or graph_ai_enabled) and (value != "ai" or ai_enabled)
    ]


def unavailable_reason(mode):
    if mode == "graph":
        return (
            "Graph answers are not configured for this application. An application owner "
            "can enable Graph retrieval in AI settings."
        )
    if mode == "ai":
        return (
            "AI answers are not configured for this application. An application owner "
            "can enable Chat conversation in AI settings."
        )
    return "Choose an available answer mode."


def requested_mode(request, graph_ai_enabled, ai_enabled=True):
    """Mode for a conversation that does not exist yet.

    AI answers when they are set up, source search when they are not: the page
    once opened on "AI · setup required" - a disabled option, selected, with
    Send enabled - when a working mode was right there. A mode the reader asked
    for by name is kept only if it can actually answer.
    """
    source = request.POST if request.method == "POST" else request.GET
    usable = dict(available_modes(graph_ai_enabled, ai_enabled))
    mode = source.get("mode", "")
    if mode in usable:
        return mode
    if request.method == "POST":
        # A send is never quietly answered some other way: a mode asked for by
        # name and not available, or none at all, is refused with the reason.
        # Only the page picks a working default - and shows it, selected.
        return mode if mode in dict(CHAT_MODES) else "ai"
    return "ai" if "ai" in usable else "search"


def wants_stream(request):
    """True when the caller asked for the streaming JSON contract."""
    return request.headers.get("X-Digital-Brain-Stream") == "1"


def failure_text(error):
    if isinstance(error, ValidationError):
        return " ".join(error.messages)
    return "AI credential is not configured. Ask an application owner to complete setup."


FOLLOWUP = re.compile(r"\b(it|that|those|they|them|this|more|continue|explain)\b", re.I)


def retrieval_query(question, history):
    """Carry topic terms into short/pronominal follow-ups; keep new topics independent."""
    if history and (FOLLOWUP.search(question) or len(question.split()) < 5):
        return question + " " + " ".join(turn.question for turn in history)
    return question


def lexical_citations(app, question, user=None):
    from . import rerank

    wide = user is not None and rerank.configuration(app.pk) is not None
    found = [
        {"id": str(entry.pk), "title": entry.title, "digest": entry.digest, "excerpt": passage}
        for _, entry, passage in retrieve(
            app, question, user, limit=rerank.CANDIDATES if wide else 5
        )
    ]
    return rerank.rerank(user, app.pk, question, found, 5) if wide else found


def answer_question(
    user, app, conversation, question, mode, graph_version=None, trim_from=None, submission=""
):
    """Produce and persist one exchange synchronously.

    This is the no-JavaScript path, and the fallback whenever streaming is
    unavailable. It raises rather than persisting a failed answer, so a provider
    error never leaves a half-finished turn in the transcript.

    `trim_from` is how regenerate and edit replace an exchange. The removal
    happens here, inside the same transaction as the new messages and only after
    the provider has answered - deleting first meant a provider failure took the
    question and every later message with it and returned nothing.
    """
    first_exchange = conversation is None
    history = conversation_context(conversation, app, user, before=trim_from)
    query = retrieval_query(question, history)
    citations = lexical_citations(app, query, user)
    answer = readable_evidence(citations)
    if mode in {"ai", "graph"}:
        from .ai import answer_with_ai, invoke_ai

        if mode == "graph":
            from .graph_ai import graph_citations

            citations = graph_citations(app.pk, query, version=graph_version, user=user)
            answer = invoke_ai(
                user, app.pk, "graph_retrieval", question, citations, history=history
            )
        else:
            answer = answer_with_ai(user, app.pk, question, citations, history=history)
    with transaction.atomic():
        access(user, app.pk, "chat")
        access(user, app.pk, "knowledge")
        if conversation is not None:
            lock_conversation(conversation)
        if trim_from is not None and conversation is not None:
            conversation.messages.filter(sequence__gte=trim_from).delete()
        if conversation is None:
            conversation = ChatConversation.objects.create(
                application=app,
                user=user,
                title=question[:120],
                mode=mode,
                graph_version=graph_version,
            )
        _, reply = record_exchange(
            app, user, conversation, question, answer, citations, mode, submission=submission
        )
        conversation.save(update_fields=["updated_at"])
        audit(user, "chat.answered", reply.pk, app.product.portfolio.organization)
    if first_exchange:
        retitle(user, app, reply.pk, question, answer)
    return conversation


def retitle(user, app, message_id, question, answer):
    """Replace the truncated first question with a generated name, once.

    Only for the opening exchange of a conversation the user has not named
    themselves. Any failure leaves the existing title alone.
    """
    from .ai import generate_title

    conversation = ChatConversation.objects.filter(messages__id=message_id).distinct().first()
    if conversation is None or conversation.title_locked:
        return None
    if conversation.messages.filter(role="assistant", status="complete").count() > 1:
        return None
    title = generate_title(user, app.pk, question, answer)
    if not title or title == conversation.title:
        return None
    ChatConversation.objects.filter(pk=conversation.pk, title_locked=False).update(title=title)
    return title


@login_required
@require_http_methods(["GET"])
def onboarding(request, pk):
    """What this application still needs, for whatever it is for.

    One page for Engineering, Operations or both: the shared connector choice,
    then each purpose's gates. Read-only, and every row links to the screen that
    fixes it rather than fixing it here: each of those screens has its own
    permission check and its own audit event, and a setup page that wrote to all
    of them would be a second way to do everything with none of that.
    """
    from .readiness import GATE_ICONS, GATES, gate_ready, outstanding, record_completion, setup
    from .services import purposes

    app, grant = access(request.user, pk, "knowledge")
    state = setup(app)
    record_completion(app, state)
    found = state.steps
    serving = purposes(app)
    # "Nothing but the switch that got you here" - derived from the application
    # rather than from a query parameter, so it still greets somebody who
    # created this last week and is only now coming back to it.
    fresh = not [
        step for step in found if step.ok and step.key not in {"code_factory", "service_ops"}
    ]
    # One sequence across both gates rather than 1-5 and then 1-4 again. The
    # number is what somebody says out loud - "I am stuck on step 6" - and two
    # sixes on one screen would make that ambiguous. Positional, so a step that
    # only appears once something is indexed does not renumber the ones above it.
    order = {step.key: index for index, step in enumerate(found, 1)}
    return render(
        request,
        "onboarding.html",
        {
            "application": app,
            "grant": grant,
            "fresh": fresh,
            "setup": state,
            "done": state.done,
            "total": state.total,
            "next_step": state.next_step,
            "next_number": order.get(getattr(state.next_step, "key", None)),
            "gates": [
                {
                    "key": key,
                    "label": label,
                    "icon": GATE_ICONS[key],
                    "blurb": blurb,
                    "steps": [(order[step.key], step) for step in found if step.gate == key],
                    "ready": gate_ready(found, key),
                    "outstanding": outstanding(found, key),
                    "done": sum(1 for step in found if step.gate == key and step.ok),
                    "count": sum(1 for step in found if step.gate == key),
                }
                for key, label, blurb, purpose in GATES
                if purpose is None or purpose in serving
            ],
            "serving": serving,
        },
    )
