"""A source's passages as rows, so the database can choose them (`KnowledgeChunk`).

Keyword search used to take the best hundred *sources* and cut every one into
passages in Python. A library of large documents meant reading megabytes to
rank a few kilobytes, and the memory and time of one question grew with the
size of the documents rather than with what it asked. Here the passages are
written once, with the source, and a question reads only the passages the
database finds for it.

**The same windows, the same ranking.** Rows are `workbench.PASSAGE_LENGTH`
characters every `PASSAGE_STEP`, exactly the slices `retrieve` cut, so embedded
passages, citations and excerpts line up with them. What this changes is which
passages are *candidates*; the ordering after that - covering, BM25, spread,
reranking - is `retrieve`'s as before. Which path answers is
`settings.RETRIEVAL_CANDIDATES`, "sources" until the scorecard shows "chunks"
no worse (`manage.py scorecard` measures both).

**Kept in step by the writes that change a source.** `triggers.py` calls
`sync` on a saved source and `KnowledgeEntryQuerySet.update` on a queryset
write, inside the same transaction, so a source and its passages commit
together. Only active sources have passages: a superseded record keeps its
text, not a second copy of it. A source whose `chunked_digest` is behind its
`digest` has none that count, and `backfill` - a maintenance step and
`manage.py chunk_sources` - catches up whatever was written before this
existed.
"""

import hashlib
import logging

from django.db import connection
from django.db.models import F, Q

from .models import KnowledgeChunk, KnowledgeEntry

logger = logging.getLogger(__name__)

#: PostgreSQL's full text for a passage: a column generated from its heading and
#: text (migration 0088), so ranking never tokenizes a passage per question.
CHUNK_TEXT = '"platform_core_knowledgechunk"."search_vector"'
#: Passages a question ranks on the chunk path. The source path ranked every
#: passage of a hundred sources; this ranks the best passages of any of them.
CANDIDATE_PASSAGES = 300
#: On SQLite there is no full-text rank to choose by, so it reads what matches.
SCAN_LIMIT = 2000
#: Sources a backfill step writes before letting the lane rest.
BACKFILL_BATCH = 25


def windows(content):
    """(ordinal, offset, text) for each passage of `content`."""
    from .workbench import PASSAGE_LENGTH, PASSAGE_STEP

    return [
        (ordinal, offset, content[offset : offset + PASSAGE_LENGTH])
        for ordinal, offset in enumerate(range(0, len(content), PASSAGE_STEP))
    ]


def write(entry):
    """Replace this source's passages with ones cut from its current text."""
    KnowledgeChunk.objects.filter(entry_id=entry.pk).delete()
    KnowledgeChunk.objects.bulk_create(
        KnowledgeChunk(
            entry_id=entry.pk,
            application_id=entry.application_id,
            ordinal=ordinal,
            offset=offset,
            heading=entry.title,
            text=text,
            digest=hashlib.sha256(text.encode()).hexdigest(),
        )
        for ordinal, offset, text in windows(entry.content)
    )
    KnowledgeEntry.objects.filter(pk=entry.pk).update(chunked_digest=entry.digest)
    entry.chunked_digest = entry.digest


def clear(entry_ids):
    KnowledgeChunk.objects.filter(entry_id__in=entry_ids).delete()
    KnowledgeEntry.objects.filter(pk__in=entry_ids).exclude(chunked_digest="").update(
        chunked_digest=""
    )


def current(entry):
    """Whether the stored passages were cut from exactly this text and title."""
    if entry.chunked_digest != entry.digest:
        return False
    first = KnowledgeChunk.objects.filter(entry_id=entry.pk, ordinal=0).first()
    if first is None:
        return not entry.content
    return first.heading == entry.title


def sync(entry):
    """Make one source's passages match it: written when active, gone when not."""
    if not entry.active:
        if entry.chunked_digest or KnowledgeChunk.objects.filter(entry_id=entry.pk).exists():
            clear([entry.pk])
        return
    if not current(entry):
        write(entry)


def sync_ids(entry_ids):
    for entry in KnowledgeEntry.objects.filter(pk__in=entry_ids):
        sync(entry)


def behind():
    """Active sources whose passages are missing or were cut from older text."""
    return KnowledgeEntry.objects.filter(active=True).exclude(chunked_digest=F("digest"))


def complete(app):
    """Whether every active source of `app` has current passages.

    What `retrieval_candidates = "auto"` asks before answering from passages: a
    source with none current is not among them, so answering from them while
    the backfill is still running would miss it rather than rank it lower.
    """
    return not behind().filter(application_id=app.pk).exists()


def backfill(limit=BACKFILL_BATCH, application_id=None):
    """Write passages for up to `limit` sources that have none current. Returns how many."""
    pending = behind()
    if application_id is not None:
        pending = pending.filter(application_id=application_id)
    count = 0
    for entry in pending.order_by("created_at", "id")[:limit]:
        write(entry)
        count += 1
    return count


def backfill_step():
    """A maintenance step: a batch at a time, and True while there was some to do."""
    count = backfill()
    if count:
        logger.info("passages written", extra={"event": "chunks_backfilled", "count": count})
    return bool(count)


def searchable(app):
    """Passages that may be answered from: active, unrestricted, and current."""
    return KnowledgeChunk.objects.filter(
        application_id=app.pk,
        entry__active=True,
        entry__chunked_digest=F("entry__digest"),
    ).exclude(entry__sensitivity="restricted")


def candidates(app, question):
    """The passages keyword search ranks for a question, with their sources.

    The source's text is deferred: ranking needs the passage, and reading every
    candidate's whole document is the cost this exists to avoid.
    """
    from .ranking import term, words
    from .workbench import full_text_query

    rows = searchable(app).select_related("entry").defer("entry__content")
    if connection.vendor == "postgresql":
        query = full_text_query(question)
        if not query:
            return []
        return list(
            rows.extra(
                select={"text_rank": f"ts_rank_cd({CHUNK_TEXT}, to_tsquery('english', %s))"},
                select_params=[query],
                where=[f"{CHUNK_TEXT} @@ to_tsquery('english', %s)"],
                params=[query],
                order_by=["-text_rank"],
            )[:CANDIDATE_PASSAGES]
        )
    condition = Q()
    for word in list(dict.fromkeys(words(question)))[:20]:
        needle = term(word) if word.startswith(term(word)) else word
        condition |= Q(text__icontains=needle) | Q(heading__icontains=needle)
    if not condition:
        return []
    return list(rows.filter(condition).order_by("entry__created_at", "ordinal")[:SCAN_LIMIT])
