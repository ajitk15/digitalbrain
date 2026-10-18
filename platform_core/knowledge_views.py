"""The Knowledge screens: the source list, one source's page, and what a
person may change there (its document type, asking AI what it is).

Split out of `workbench`, which keeps writing knowledge and retrieving
evidence; these views only read through it and its access check.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import KnowledgeEntry
from .services import audit
from .workbench import access


@login_required
@require_http_methods(["GET", "POST"])
def knowledge(request, pk):
    """The old sources list, now the documents page under its own URL.

    The check runs here as well as in `documents` so a caller with no grant gets
    the same 404 from either URL, rather than one of them confirming the
    application exists by the shape of its refusal.
    """
    access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        return HttpResponseNotAllowed(["GET"])
    from .documents import documents

    return documents(request, pk)


def set_document_type(request, app, entry):
    """A person says what kind of document this is, or hands it back to the rules.

    Documents only - a ticket has its own score. The choice is a registry key or
    blank; anything else is refused rather than stored. The check is worked out
    again at once, so the page shows the document judged as what it now is.
    """
    from .document_ai import cached_type
    from .document_types import BY_KEY
    from .source_quality import run as check_source

    chosen = request.POST.get("document_type", "")
    if not entry.document_id or (chosen and chosen not in BY_KEY):
        return HttpResponse("Unknown document type.", status=400)
    document = entry.document
    with transaction.atomic():
        entry.document_type = chosen
        entry.quality = check_source(entry.content, document.conversion_report, entry.title,
                                     chosen, cached_type(app.pk, entry.digest))
        entry.save(update_fields=["document_type", "quality"])
        audit(request.user, "knowledge.type_set", entry.pk, app.product.portfolio.organization,
              {"document_type": chosen or "detected"})
    messages.success(
        request,
        f"Checked as {BY_KEY[chosen].label.lower()}." if chosen
        else "Checked as the type the rules detect.",
    )
    return back_to_source(request, app, entry)


def back_to_source(request, app, entry):
    if request.POST.get("next") == "document" and entry.document_id:
        return redirect("document-detail", pk=app.pk, document_id=entry.document_id)
    return redirect("knowledge-detail", pk=app.pk, entry_id=entry.pk)


def ask_ai_about_document(request, app, grant, entry, action):
    """The two document AI buttons (`document_ai`): what kind of document this is,
    when the rules could not say, and an advisory review. Plain POSTs; each
    spends through `invoke_ai`, so its grant check and the budget decide."""
    from . import document_ai

    if not entry.document_id:
        return HttpResponse("Only documents have a type.", status=400)
    if action == "ai_review" and grant.role != "owner":
        raise PermissionDenied
    try:
        if action == "ai_type":
            verdict = document_ai.classify(request.user, entry)
            if verdict is None:
                messages.info(request, "Nothing to ask: the type is already settled, or "
                                       "Document typing is off in AI settings.")
            elif verdict["verdict"] == "accepted":
                messages.success(request, f"AI named it {verdict['label'].lower()}, quoting "
                                          "lines found in the document.")
            else:
                messages.warning(request, f"Left as Other: {verdict['reason']}.")
            details = {"verdict": verdict["verdict"] if verdict else "not asked",
                       "type": verdict["key"] if verdict else ""}
        else:
            found = document_ai.review(request.user, entry)
            messages.success(request, f"AI review: {len(found['findings'])} finding(s), each "
                                      "quoted from the document.")
            details = {"findings": len(found["findings"]), "dropped": found["dropped"]}
    except (ValidationError, ImproperlyConfigured) as error:
        messages.error(request, " ".join(getattr(error, "messages", None) or [str(error)]))
        return back_to_source(request, app, entry)
    audit(request.user, f"knowledge.{action}", entry.pk, app.product.portfolio.organization,
          details)
    return back_to_source(request, app, entry)


@login_required
@require_http_methods(["GET", "POST"])
def knowledge_detail(request, pk, entry_id):
    app, grant = access(request.user, pk, "knowledge")
    entry = get_object_or_404(KnowledgeEntry, pk=entry_id, application=app, active=True)
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        action = request.POST.get("action", "archive")
        if action in {"restrict", "unrestrict"}:
            from .sensitivity import RESTRICTED, STANDARD

            # Anyone who may change knowledge may restrict; only an owner may
            # lift it, the direction that hands the source back to a model.
            if action == "unrestrict" and grant.role != "owner":
                raise PermissionDenied
            with transaction.atomic():
                # Every revision of the record, so an older one that a
                # published graph still points at is covered as well.
                KnowledgeEntry.objects.filter(
                    application=app,
                    **({"source": entry.source} if entry.source else {"pk": entry.pk}),
                ).update(sensitivity=RESTRICTED if action == "restrict" else STANDARD)
                audit(
                    request.user,
                    f"knowledge.{action}ed",
                    entry.pk,
                    app.product.portfolio.organization,
                )
            messages.success(
                request,
                "Restricted: AI will no longer read or cite this source."
                if action == "restrict"
                else "AI may use this source again.",
            )
            return redirect("knowledge-detail", pk=pk, entry_id=entry.pk)
        if action == "set_type":
            return set_document_type(request, app, entry)
        if action in {"ai_type", "ai_review"}:
            return ask_ai_about_document(request, app, grant, entry, action)
        if action != "archive":
            return HttpResponse("Unknown action.", status=400)
        with transaction.atomic():
            entry.active = False
            entry.save(update_fields=["active"])
            audit(request.user, "knowledge.archived", entry.pk, app.product.portfolio.organization)
        messages.success(request, "Source archived. New answers will exclude it.")
        return redirect("knowledge", pk=pk)
    from .document_ai import panel as document_ai_panel
    from .document_types import CHOICES as TYPE_CHOICES
    from .sensitivity import category as personal_category
    from .sensitivity import summary as personal_summary
    from .source_library import entry_origin, graph_usage, ticket_quality
    from .source_quality import for_entries
    from .ticket_library import ticket_entries
    from .ticket_quality import parse as parse_ticket

    origin = entry_origin(app, entry) if not entry.document_id else None
    quality = ticket_quality(entry, origin) if origin else None
    # Where "Back to" leads: a ticket came from the Tickets tab.
    is_ticket = bool(origin) and ticket_entries(app).filter(pk=entry.pk).exists()
    source_check = for_entries([entry])[entry.pk] if entry.document_id else None
    # A scored ticket is shown as a ticket: its Jira fields as chips and its
    # description as text, rather than the raw stored record.
    fields, description = parse_ticket(entry.content) if quality else ({}, "")
    return render(
        request,
        "knowledge_detail.html",
        {
            "application": app,
            "entry": entry,
            "origin": origin,
            "quality": quality,
            "source_check": source_check,
            "type_choices": TYPE_CHOICES,
            "document_ai": document_ai_panel(app, grant, entry, source_check),
            "ticket_fields": [
                (label, fields[key])
                for key, label in (
                    ("type", "Type"),
                    ("status", "Status"),
                    ("priority", "Priority"),
                    ("labels", "Labels"),
                )
                if fields.get(key)
            ],
            "ticket_key": fields.get("key", ""),
            "is_ticket": is_ticket,
            "ticket_description": description,
            "source_link": entry.source if entry.source.startswith(("https://", "http://")) else "",
            "versions": graph_usage(app, [entry]).get(str(entry.pk), []),
            "personal_data": personal_summary(entry.personal_data),
            "personal_category": personal_category(entry.personal_data),
            "masked": personal_summary(entry.masked),
            "grant": grant,
        },
    )
