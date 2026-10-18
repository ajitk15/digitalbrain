"""Search embeddings: where to start looking, by meaning - never what is cited.

BM25 finds a relationship or passage only when it shares words with the
question. "Why can a partner download records of people who said no?" shares
none with the ticket that answers it (CARE-401, about consent on export). An
embedding places texts by meaning, so the nearest ones can be starting points
even with no word in common.

What it does not change: a vector only picks candidates. Everything cited is
still a graph relationship or a passage, verified against its live source
(active, same digest, exact quote) exactly as before. A vector is never
evidence, and a model never sees one.

Spend follows the platform's rules. Vectors come from OpenAI through the
application's own credential (Anthropic has no embeddings endpoint), every
request writes an `AIUsage` receipt with its token count, and nothing is
embedded on its own: relationships and passages when someone requests an AI
graph run (or an operator runs `manage.py embed`), a question when it is asked.
Stored vectors are content-addressed (`models.Embedding`), so unchanged text
is never paid for twice and the benchmark reads only what is stored.

Search is exact cosine similarity in this process, over unit-length float32
vectors with numpy: a few thousand vectors of 512 dimensions is a few
megabytes and well under a millisecond per question. pgvector would replace
the in-memory matrix once a library outgrows that, and nothing else.
"""

import hashlib
import logging
import threading
import uuid
from collections import OrderedDict
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.http import Http404

logger = logging.getLogger(__name__)

#: Asked of the model (text-embedding-3 accepts `dimensions`): a third of the
#: default size, a third of the storage and memory, little loss for ranking.
DIMENSIONS = 512
#: Texts per request: far under OpenAI's 2,048 inputs and 300,000 tokens.
BATCH = 256
#: Longest text embedded. A relationship is a sentence and a passage is 1,400
#: characters, so this only ever trims a pasted essay of a question.
MAX_TEXT_CHARACTERS = 4000
#: Texts one indexing run may embed, so a huge library cannot run up a bill
#: from one button. 12,000 short texts is roughly 0.4 million tokens.
MAX_INDEX = 12000
TIMEOUT = 60
#: Nearest texts taken as starting points, and the least similarity counted.
#: Below the floor a "nearest" text is only the least unrelated one.
NEAREST = 30
MIN_SIMILARITY = 0.2
#: Reciprocal rank fusion's constant: the usual 60, so a text ranked first by
#: one signal does not drown a text ranked well by both.
FUSION_K = 60


def configuration(app_id):
    """The enabled embeddings configuration, or None: the feature is opt-in."""
    from .models import AIConfiguration

    return (
        AIConfiguration.objects.filter(
            application_id=app_id, purpose="embeddings", enabled=True, provider="openai"
        )
        .exclude(model="")
        .first()
    )


def clip(text):
    return " ".join(text.split())[:MAX_TEXT_CHARACTERS]


def digest(text):
    """The key a text's vector is stored under: the text exactly as embedded."""
    return hashlib.sha256(text.encode()).hexdigest()


def _http_client():
    from openai import DefaultHttpxClient

    return DefaultHttpxClient(follow_redirects=False, trust_env=False)


def request(token, model, texts):
    """One embeddings request: (unit vectors, provider request id, input tokens).

    Strict like every adapter here: a missing or malformed vector or token
    count is an error, never a vector of zeros or a free request.
    """
    import numpy as np
    from openai import OpenAI

    from .agent_runtime.usage import checked_count, checked_request_id

    with OpenAI(
        api_key=token,
        base_url="https://api.openai.com/v1",
        organization="",
        project="",
        max_retries=0,
        timeout=TIMEOUT,
        http_client=_http_client(),
    ) as client:
        raw = client.embeddings.with_raw_response.create(
            model=model, input=texts, dimensions=DIMENSIONS, encoding_format="float"
        )
        response = raw.parse()
        request_id = checked_request_id(
            raw.headers.get("x-request-id") or f"embeddings-{uuid.uuid4()}"
        )
    if response.usage is None:
        raise ValueError("Missing usage")
    tokens = checked_count(response.usage.prompt_tokens)
    rows = sorted(response.data, key=lambda item: item.index)
    if [item.index for item in rows] != list(range(len(texts))):
        raise ValueError("Embedding count does not match the request")
    vectors = []
    for item in rows:
        vector = np.asarray(item.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.shape != (DIMENSIONS,) or not np.isfinite(norm) or norm == 0:
            raise ValueError("Invalid embedding")
        vectors.append(vector / norm)
    return vectors, request_id, tokens


def embed(user, app, items, write=False):
    """Embed and store whichever of these texts are not stored yet. Returns how many.

    `items` are (text, kind, entry, offset), text already `clip`ped. The caller's
    access is checked here, as `invoke_ai` checks it: `write` for indexing a
    graph, read for asking a question. Each request is receipted as soon as it
    returns - the provider has billed it whether or not storing succeeds.
    """
    from openai import OpenAIError

    from . import gateway
    from .ai import provider_credential
    from .models import Embedding
    from .services import record_ai_usage
    from .workbench import access

    access(user, app.pk, "knowledge", write=write)
    config = configuration(app.pk)
    if config is None:
        raise ValidationError("Search embeddings are not switched on in AI settings.")
    wanted = {}
    for text, kind, entry, offset in items:
        if text:
            wanted.setdefault(digest(text), (text, kind, entry, offset))
    have = set(stored_digests(app.pk, config.model, list(wanted)))
    missing = [(key, *value) for key, value in wanted.items() if key not in have][:MAX_INDEX]
    if not missing:
        return 0
    from .budget import check as check_budget

    token = provider_credential(config, app)
    created = 0
    for start in range(0, len(missing), BATCH):
        batch = missing[start : start + BATCH]
        # Per request, not once: a large index can cross the cap part way.
        check_budget(app.pk)
        texts = [row[1] for row in batch]
        with gateway.admitted(
            user, app, config, "embeddings", prompt_characters=sum(map(len, texts))
        ):
            try:
                vectors, request_id, tokens = request(token, config.model, texts)
            except (OpenAIError, OSError, ValueError, TypeError):
                raise ValidationError(
                    "Embedding request failed. Check the embedding model, the OpenAI "
                    "credential and provider limits. It is not retried automatically."
                ) from None
            record_ai_usage(
                actor=user,
                application_id=app.pk,
                provider="OpenAI",
                model=config.model,
                request_id=request_id,
                purpose="embeddings",
                input_tokens=tokens,
                output_tokens=0,
                amount=(Decimal(tokens) * config.input_rate / Decimal(1000000)).quantize(
                    Decimal("0.00000001")
                ),
                estimated=True,
                authorized=app,
            )
        Embedding.objects.bulk_create(
            [
                Embedding(
                    application=app,
                    model=config.model,
                    dimensions=DIMENSIONS,
                    digest=key,
                    kind=kind,
                    vector=vector.astype("<f4").tobytes(),
                    entry=entry,
                    offset=offset,
                )
                for (key, _, kind, entry, offset), vector in zip(batch, vectors, strict=True)
            ],
            ignore_conflicts=True,
        )
        created += len(batch)
    forget(app.pk)
    return created


def _rows(app_id, model, **filters):
    from .models import Embedding

    return Embedding.objects.filter(
        application_id=app_id, model=model, dimensions=DIMENSIONS, **filters
    )


def stored_digests(app_id, model, digests):
    found = []
    for start in range(0, len(digests), 500):
        found += _rows(app_id, model, digest__in=digests[start : start + 500]).values_list(
            "digest", flat=True
        )
    return found


def stored_vectors(app_id, model, digests):
    """{digest: unit vector} for whichever of these texts are stored."""
    import numpy as np

    found = {}
    for start in range(0, len(digests), 500):
        for key, data in _rows(app_id, model, digest__in=digests[start : start + 500]).values_list(
            "digest", "vector"
        ):
            found[key] = np.frombuffer(bytes(data), dtype="<f4")
    return found


def question_vector(user, app_id, question):
    """(vector, model) for a question, or (None, None) to rank by words alone.

    A stored vector is free and always used. Otherwise one is requested only
    when there is someone to charge it to and the feature is on. A failure
    never stops an answer - BM25 still answers, as it did before embeddings -
    but it is logged, so a broken credential does not go unnoticed.
    """
    config = configuration(app_id)
    text = clip(question or "")
    if config is None or not text:
        return None, None
    key = digest(text)
    vector = stored_vectors(app_id, config.model, [key]).get(key)
    if vector is None and user is not None:
        from .models import Application

        try:
            embed(user, Application.objects.get(pk=app_id), [(text, "question", None, None)])
        except (ValidationError, ImproperlyConfigured, PermissionDenied, Http404):
            logger.warning("embedding_unavailable", extra={"event": "embedding_unavailable"})
            return None, None
        vector = stored_vectors(app_id, config.model, [key]).get(key)
    return (vector, config.model) if vector is not None else (None, None)


def nearest(matrix, vector, limit=NEAREST):
    """Row numbers of the most similar vectors, best first, above the floor."""
    import numpy as np

    if matrix is None or not len(matrix):
        return []
    scores = matrix @ vector
    count = min(limit, len(scores))
    best = np.argpartition(-scores, count - 1)[:count]
    best = sorted(best, key=lambda row: (-scores[row], row))
    return [int(row) for row in best if scores[row] >= MIN_SIMILARITY]


def fuse(*rankings):
    """Reciprocal rank fusion: one order from several, each a list of ids.

    An id ranked well by both words and meaning rises above one ranked first
    by either alone. Ties keep the first ranking's order, so with no vector
    the result is exactly the word ranking.
    """
    scores, first = {}, {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (FUSION_K + rank + 1)
            first.setdefault(item, len(first))
    return sorted(scores, key=lambda item: (-scores[item], first[item]))


# ── relationship vectors for one prepared graph ───────────────────────────────


def relation_matrix(app_id, model, ready):
    """(candidate positions, matrix) of a prepared graph's embedded relationships.

    Kept on the prepared graph, keyed by how many vectors the application has,
    so a later indexing run is picked up on the next question.
    """
    import numpy as np

    marker = _rows(app_id, model, kind="relation").count()
    with _lock:
        cached = getattr(ready, "vectors", {}).get(model)
    if cached and cached[0] == marker:
        return cached[1], cached[2]
    keys = [digest(clip(text)) for _, text in ready.candidates]
    found = stored_vectors(app_id, model, list(dict.fromkeys(keys)))
    positions = [position for position, key in enumerate(keys) if key in found]
    matrix = np.vstack([found[keys[p]] for p in positions]) if positions else None
    with _lock:
        if not hasattr(ready, "vectors"):
            ready.vectors = {}
        ready.vectors[model] = (marker, positions, matrix)
    return positions, matrix


# ── passage vectors for one application ───────────────────────────────────────

_lock = threading.Lock()
_passages = OrderedDict()
PASSAGE_CACHE = 8


def passage_matrix(app_id, model):
    """(rows, matrix) of the application's stored passage vectors.

    Each row is (entry id, offset, digest), so a hit can be re-read from its
    source and re-checked: a passage whose text has changed since it was
    embedded no longer has that digest, and is skipped.
    """
    import numpy as np

    marker = _rows(app_id, model, kind="passage").count()
    key = (str(app_id), model)
    with _lock:
        cached = _passages.get(key)
        if cached and cached[0] == marker:
            _passages.move_to_end(key)
            return cached[1], cached[2]
    rows, vectors = [], []
    for entry_id, offset, text_digest, data in _rows(app_id, model, kind="passage").values_list(
        "entry_id", "offset", "digest", "vector"
    ):
        if entry_id is None or offset is None:
            continue
        rows.append((entry_id, offset, text_digest))
        vectors.append(np.frombuffer(bytes(data), dtype="<f4"))
    matrix = np.vstack(vectors) if vectors else None
    with _lock:
        _passages[key] = (marker, rows, matrix)
        while len(_passages) > PASSAGE_CACHE:
            _passages.popitem(last=False)
    return rows, matrix


def forget(app_id):
    with _lock:
        for key in [key for key in _passages if key[0] == str(app_id)]:
            del _passages[key]


# ── indexing ──────────────────────────────────────────────────────────────────


def passage_text(entry, offset):
    """The text a passage is embedded as - and re-checked against at search."""
    from .workbench import PASSAGE_LENGTH

    return clip(f"{entry.title} {entry.content[offset : offset + PASSAGE_LENGTH]}")


def index(user, app, data, entries):
    """Embed a graph version's relationships, its sources' passages and the
    benchmark questions. Returns counts for the quality report.

    Relationships first: they are what graph answers start from. Then
    passages, which Chat's AI mode and the search tool start from. Questions
    last, so the benchmark can measure meaning without spending anything when
    it runs.
    """
    from .graph_ai import prepare
    from .models import BenchmarkQuestion
    from .workbench import PASSAGE_STEP

    ready = prepare(data)
    relations = [(clip(text), "relation", None, None) for _, text in ready.candidates]
    passages = [
        (passage_text(entry, offset), "passage", entry, offset)
        for entry in entries
        for offset in range(0, len(entry.content), PASSAGE_STEP)
    ]
    questions = [
        (clip(text), "question", None, None)
        for text in BenchmarkQuestion.objects.filter(application=app).values_list(
            "question", flat=True
        )
    ]
    items = (relations + passages + questions)[:MAX_INDEX]
    created = embed(user, app, items, write=True)
    config = configuration(app.pk)
    have = set(stored_digests(app.pk, config.model, [digest(item[0]) for item in items]))
    return {
        "model": config.model,
        "dimensions": DIMENSIONS,
        "relationships": sum(digest(item[0]) in have for item in relations),
        "relationships_total": len(relations),
        "passages": sum(digest(item[0]) in have for item in passages),
        "passages_total": len(passages),
        "new": created,
    }
