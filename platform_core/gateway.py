"""The AI gateway: what every paid model call passes through, in one place.

Phase 4 of the scale work, and built here rather than bought. A proxy such as
LiteLLM or a cloud AI gateway holds provider keys itself and keeps its own
prices and retry rules; this platform's rules are that credentials are files
per application, every provider request leaves one `AIUsage` receipt, a budget
is checked before every call and a paid call is never retried. So the gateway
is a pipeline in front of the SDKs already used, not a service beside them:

    purpose -> authorize -> configuration -> admit -> credential -> adapter
            -> receipt -> settle -> authorize again -> event

**Purposes are a closed registry** (`PURPOSES`), like `services.FEATURES`:
which switches a purpose needs, and which roles may ask for it. The rules used
to be an if/elif chain in `ai.invoke_ai`, written once before the call and
again, slightly differently, after it.

**Admission holds a place for the call** (`models.AIAdmission`, one row per
paid call in flight). It reserves the call's most expensive outcome against
the monthly cap, so two calls at the cap no longer both start: each sees the
other's reservation. And it is a slot: at most `max_calls_per_application`
calls in flight for one application and `max_calls_per_provider` across the
deployment, so one application's batch cannot take every connection a provider
allows. A caller waits up to `slot_wait_seconds` for a slot, then is told the
provider is busy - nothing was sent, so nothing was billed. A settled admission
is deleted; one left by a process that died expires after `admission_minutes`
and is swept by the maintenance lane.

The cap keeps its meaning: a call starts while recorded spend plus the
reservations of calls in flight is under the cap, and a call already started
is finished. With one call at a time that is exactly the check it always was.

Admission takes its locks in a short transaction of its own, so no lock is
held across a provider call - callers call the gateway outside a transaction,
as every one does.
"""

import hashlib
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from . import metrics, telemetry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Purpose:
    """Who may spend on one kind of model call.

    `checks` are `(feature, write)` pairs passed to `workbench.access`, after the
    knowledge access every purpose needs; `roles`, when set, are the only grant
    roles that may ask.
    """

    key: str
    checks: tuple = ()
    roles: frozenset = field(default_factory=frozenset)


PURPOSES = {
    purpose.key: purpose
    for purpose in (
        Purpose("chat", (("chat", False),)),
        Purpose("graph_retrieval", (("chat", False),)),
        Purpose("conversation_title", (("chat", False),)),
        Purpose(
            "graph_generation", (("knowledge", True),), frozenset({"owner", "contributor"})
        ),
        Purpose("plan_drafting", (("code_factory", True),)),
        Purpose("serviceops_triage", (("service_ops", True),)),
        # Reorders evidence already verified for this user: reading it is enough.
        Purpose("reranking"),
        # Runs as whoever converted the document, or pressed the button: someone
        # who may change knowledge, as setting a type by hand needs.
        Purpose("document_typing", roles=frozenset({"owner", "contributor"})),
        Purpose("document_review", roles=frozenset({"owner"})),
        # Reads Code Graph's snapshots; pressed by someone who may index them.
        Purpose("code_review", (("code_graph", False), ("code_review", True))),
        Purpose("code_docs", (("code_graph", False), ("code_docs", True))),
        # Embeddings check their own write access: indexing writes, a question reads.
        Purpose("embeddings"),
    )
}


def authorize(user, app_id, purpose):
    """(application, grant) when `user` may spend on `purpose` here; raises otherwise.

    Called before a call and again after it: a grant revoked while a model was
    answering keeps the receipt and loses the answer.
    """
    from .policy import application_for
    from .workbench import access

    rule = PURPOSES.get(purpose)
    if rule is None:
        raise ValidationError("Unsupported AI purpose.")
    app, grant = application_for(user, app_id)
    access(user, app_id, "knowledge")
    if rule.roles and grant.role not in rule.roles:
        raise PermissionDenied
    for feature, write in rule.checks:
        access(user, app_id, feature, write=write)
    return app, grant


# ── admission ─────────────────────────────────────────────────────────────────


class ProviderBusy(ValidationError):
    """Every slot for this application or provider stayed taken; nothing was sent."""


def limits():
    return {
        "application": settings.AI_GATEWAY["max_calls_per_application"],
        "provider": settings.AI_GATEWAY["max_calls_per_provider"],
        "wait": settings.AI_GATEWAY["slot_wait_seconds"],
        "minutes": settings.AI_GATEWAY["admission_minutes"],
    }


#: Characters a token stands for, at the pessimistic end, for the reservation.
CHARACTERS_PER_TOKEN = 3


def estimate(config, prompt_characters, max_tokens):
    """The most a call can cost at the entered rates: every prompt character a
    third of a token, and the whole reply cap spent."""
    prompt = Decimal(prompt_characters // CHARACTERS_PER_TOKEN + 1)
    reply = Decimal(max_tokens or 0)
    return ((prompt * config.input_rate + reply * config.output_rate) / Decimal(1000000)).quantize(
        Decimal("0.00000001")
    )


def provider_lock(provider):
    """Serialize admissions to one provider across applications, on PostgreSQL.

    The application row already serializes one application's admissions; this
    makes the deployment-wide count exact too. SQLite writes one at a time anyway.
    """
    if connection.vendor == "postgresql":
        key = int.from_bytes(hashlib.sha256(f"ai:{provider}".encode()).digest()[:8], "big") >> 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def try_admit(app, config, purpose, user, amount):
    """An admission row, or None when no slot is free. Raises BudgetExceeded."""
    from .budget import BudgetExceeded, month_start, spent
    from .models import AIAdmission, AIBudget, Application
    from .policy import LIVE

    now = timezone.now()
    bound = limits()
    with transaction.atomic():
        # The application row is the lock a delete takes too (`hierarchy.delete`),
        # so a call is either admitted before it or refused after it; and an
        # application disabled since `authorize` read it is refused here.
        locked = list(
            Application.objects.select_for_update(of=("self",))
            .filter(pk=app.pk, **LIVE)
            .values_list("pk")
        )
        if not locked:
            raise PermissionDenied("This application is disabled. Nothing was sent.")
        provider_lock(config.provider)
        live = AIAdmission.objects.filter(expires_at__gt=now)
        mine = live.filter(application_id=app.pk)
        budget = AIBudget.objects.filter(application_id=app.pk).first()
        if budget is not None:
            reserved = mine.aggregate(total=Sum("reserved"))["total"] or Decimal("0")
            used = spent(app.pk)
            if used + reserved >= budget.monthly_limit:
                raise BudgetExceeded(
                    f"This application has used its AI budget for {month_start():%B %Y}: "
                    f"${used:.2f} of ${budget.monthly_limit:.2f}"
                    + (f", with ${reserved:.2f} held by calls in flight" if reserved else "")
                    + ". An application owner can raise it in AI settings; it resets on the 1st."
                )
        if (
            mine.count() >= bound["application"]
            or live.filter(provider=config.provider).count() >= bound["provider"]
        ):
            return None
        return AIAdmission.objects.create(
            application_id=app.pk,
            purpose=purpose,
            provider=config.provider,
            actor=user,
            reserved=amount if budget is not None else Decimal("0"),
            expires_at=now + timedelta(minutes=bound["minutes"]),
        )


#: Seconds between looks for a free slot.
POLL = 0.25


@contextmanager
def admitted(user, app, config, purpose, prompt_characters=0, max_tokens=0):
    """Hold a slot and a reservation for one paid call, released however it ends.

    Traced as one span from the first try for a slot to the release
    (`telemetry`): how long admission waited, the outcome, and each receipt
    the call wrote - ids and counts, never what was sent or answered.
    """
    with telemetry.span(
        f"ai {purpose}",
        purpose=purpose,
        provider=config.provider,
        model=config.model,
        application_id=app.pk,
    ) as traced:
        with _admitted(
            user, app, config, purpose, prompt_characters, max_tokens, traced
        ) as admission:
            yield admission


@contextmanager
def _admitted(user, app, config, purpose, prompt_characters, max_tokens, traced):
    from .models import AIAdmission

    amount = estimate(config, prompt_characters, max_tokens)
    waiting = time.monotonic()
    deadline = waiting + limits()["wait"]
    while True:
        admission = try_admit(app, config, purpose, user, amount)
        if admission is not None:
            break
        if time.monotonic() >= deadline:
            logger.warning(
                "ai_gateway_busy",
                extra={"event": "ai_gateway_busy", "purpose": purpose, "provider": config.provider},
            )
            traced.set(outcome="busy")
            metrics.record("digitalbrain.ai.admission.wait", time.monotonic() - waiting,
                           purpose=purpose, provider=config.provider, outcome="busy")
            raise ProviderBusy(
                "The AI provider is busy with this application's other requests. Nothing was "
                "sent and nothing was charged; try again in a moment."
            )
        time.sleep(POLL)
    started = time.monotonic()
    traced.set(wait_ms=round((started - waiting) * 1000))
    metrics.record("digitalbrain.ai.admission.wait", started - waiting,
                   purpose=purpose, provider=config.provider, outcome="admitted")
    outcome = "failed"
    try:
        yield admission
        outcome = "answered"
    finally:
        traced.set(outcome=outcome)
        metrics.record("gen_ai.client.operation.duration", time.monotonic() - started,
                       purpose=purpose, provider=config.provider, model=config.model,
                       outcome=outcome)
        AIAdmission.objects.filter(pk=admission.pk).delete()
        logger.info(
            "ai_gateway_call",
            extra={
                "event": "ai_gateway_call",
                "purpose": purpose,
                "provider": config.provider,
                "model": config.model,
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )


def sweep():
    """Drop admissions a process that died left behind. A maintenance step."""
    from .models import AIAdmission

    gone, _ = AIAdmission.objects.filter(expires_at__lte=timezone.now()).delete()
    if gone:
        logger.info("ai_gateway_swept", extra={"event": "ai_gateway_swept", "count": gone})
    return False


def in_flight():
    """{provider: calls in flight}, for queue_status: counts, never who or what."""
    from django.db.models import Count

    from .models import AIAdmission

    return dict(
        AIAdmission.objects.filter(expires_at__gt=timezone.now())
        .values_list("provider")
        .annotate(calls=Count("id"))
        .order_by("provider")
    )
