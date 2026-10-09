"""A monthly AI spending cap per application, checked before every paid call.

Every model call already writes an `AIUsage` receipt; nothing stopped the
next one. Embeddings made that sharper - once switched on, every new question
spends - so an owner can now set a monthly cap, and every paid path refuses
once this calendar month's recorded spend has reached it.

What it is and is not:

- **Checked before each call, never mid-call.** A call already started is
  finished and receipted, so the month can end a single call over the cap.
  Two calls racing at the cap can both start. It is a cap on starting new
  spend, not a guarantee to the cent.
- **Measured from receipts at the rates an owner entered.** A purpose priced
  at zero spends nothing as far as the cap can tell - the AI settings page
  says so next to the cap.
- **Off unless set.** No `AIBudget` row means no cap: an application that
  never sets one behaves exactly as before.

The three choke points are `ai.invoke_ai` (chat, graph extraction, Code
Factory, triage, titles), `ai.chat_configuration` (streamed chat) and
`embeddings.embed`. A new paid path must call `check` too.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone


class BudgetExceeded(ValidationError):
    """This month's spend has reached the application's cap."""


def month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent(app_id, since=None):
    """Recorded spend this calendar month (UTC), in the receipts' currency."""
    from .models import AIUsage

    total = AIUsage.objects.filter(
        application_id=app_id, created_at__gte=since or month_start()
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


def status(app_id):
    """{limit, spent, remaining, percent} for the AI settings and usage pages, or None."""
    from .models import AIBudget

    budget = AIBudget.objects.filter(application_id=app_id).first()
    if budget is None:
        return None
    used = spent(app_id)
    return {
        "limit": budget.monthly_limit,
        "spent": used,
        "remaining": max(budget.monthly_limit - used, Decimal("0")),
        "percent": min(int(used * 100 / budget.monthly_limit), 100)
        if budget.monthly_limit
        else 100,
        "reached": used >= budget.monthly_limit,
        "set_by": budget.set_by,
        "updated_at": budget.updated_at,
    }


def check(app_id):
    """Refuse a new paid call once this month's spend has reached the cap."""
    current = status(app_id)
    if current and current["reached"]:
        raise BudgetExceeded(
            f"This application has used its AI budget for {month_start():%B %Y}: "
            f"${current['spent']:.2f} of ${current['limit']:.2f}. "
            "An application owner can raise it in AI settings; it resets on the 1st."
        )
