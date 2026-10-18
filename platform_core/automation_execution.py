"""Real ServiceOps execution: authorized queue, adapters and verifiable run receipts."""

import hashlib
import json
import uuid
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .api_auth import ApiError, authenticate
from .automation_adapters import ADAPTERS, LOCAL_JOBS, LaunchUnknown
from .connector_kinds import https_base
from .fetching import FetchError
from .models import Application, AutomationConnection, AutomationRun
from .ops_graph import automation_queryset, automation_usable
from .serviceops import fields_and_description, incident_queryset, verified
from .services import audit
from .workbench import access, add_knowledge

ACTIVE = {"queued", "launching", "running"}


def owner_access(user, pk):
    app, grant = access(user, pk, "service_ops")
    access(user, pk, "knowledge")
    if grant.role != "owner":
        raise PermissionDenied("An application owner must invoke real automation.")
    return app, grant


def connection_digest(connection):
    values = {key: getattr(connection, key) for key in (
        "provider", "base_url", "allowed_jobs", "enabled", "require_approval")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class ConnectionForm(forms.Form):
    name = forms.CharField(max_length=100, initial="External AWX")
    base_url = forms.CharField(max_length=500, label="AWX HTTPS origin",
                               help_text="Example: https://awx.company.example")
    allowed_jobs = forms.CharField(max_length=500, label="Allowed job template IDs",
                                   help_text="Comma-separated numeric IDs. Other jobs cannot run.")
    require_approval = forms.BooleanField(required=False, initial=True,
        label="Require approval by a different application owner")

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])

    def clean_allowed_jobs(self):
        values = list(dict.fromkeys(value.strip() for value in
                                    self.cleaned_data["allowed_jobs"].split(",")))
        if not values or len(values) > 30 or any(
                not value.isascii() or not value.isdecimal() or
                len(value) > 10 or int(value) <= 0 for value in values):
            raise forms.ValidationError("Provide up to 30 positive numeric template IDs.")
        return values


@transaction.atomic
def enable_local(user, pk):
    app, _ = owner_access(user, pk)
    access(user, pk, "knowledge", write=True)
    Application.objects.select_for_update().get(pk=app.pk)
    connection = AutomationConnection.objects.filter(application=app, provider="local").first()
    if connection is None:
        connection = AutomationConnection.objects.create(application=app, created_by=user,
            provider="local", name="Local ServiceOps runbooks", allowed_jobs=list(LOCAL_JOBS),
            require_approval=False)
    else:
        connection.enabled = True
        connection.allowed_jobs = list(LOCAL_JOBS)
        connection.save(update_fields=["enabled", "allowed_jobs"])
    for key, (name, description) in LOCAL_JOBS.items():
        source = f"local-runbook:{key}"
        if not automation_queryset(app).filter(source=source).exists():
            add_knowledge(user, app.pk, name, f"{name}\n\nType: Automation\n"
                f"Platform: Digital Brain Local\nAutomation ID: {key}\nState: Active\n"
                f"Risk: low\nApproval: none\n\n{description}", source=source)
    audit(user, "automation.local_enabled", connection.pk, app.product.portfolio.organization)
    return connection


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Provide valid job, connection and request identifiers.") from None


@transaction.atomic
def queue_execution(user, pk, automation_id, connection_id, key, incident_id=None):
    app, _ = owner_access(user, pk)
    Application.objects.select_for_update().get(pk=app.pk)
    key = _uuid(key)
    previous = AutomationRun.objects.filter(application=app, idempotency_key=key).first()
    if previous:
        if (str(previous.automation_id), str(previous.connection_id),
            str(previous.incident_id or ""), previous.requested_by_id) != (
                str(_uuid(automation_id)), str(_uuid(connection_id)),
                str(_uuid(incident_id)) if incident_id else "", user.pk):
            raise ValidationError("This request key belongs to a different invocation.")
        return previous
    automation = get_object_or_404(automation_queryset(app), pk=_uuid(automation_id))
    connection = get_object_or_404(AutomationConnection, pk=_uuid(connection_id),
                                  application=app, enabled=True)
    fields, _ = fields_and_description(automation)
    job = fields.get("Automation ID", "")
    platform = {"local": "Digital Brain Local", "awx": "AWX"}.get(connection.provider)
    if (not verified(automation) or not automation_usable(fields)
            or fields.get("Platform") != platform or job not in connection.allowed_jobs):
        raise ValidationError("This connection does not permit that active automation.")
    if connection.provider == "local" and job not in LOCAL_JOBS:
        raise ValidationError("This local runbook is not registered.")
    if connection.provider == "awx" and (not job.isascii() or not job.isdecimal()):
        raise ValidationError("AWX requires a numeric template ID.")
    if connection.provider == "awx" and https_base(connection.base_url) != connection.base_url:
        raise ValidationError("The configured AWX origin is invalid.")
    incident, inputs = None, {}
    if incident_id:
        incident = get_object_or_404(incident_queryset(app), pk=_uuid(incident_id))
        if not verified(incident):
            raise ValidationError("The incident source changed; refresh it before invoking.")
        incident_fields, _ = fields_and_description(incident)
        inputs = {target: incident_fields[source] for source, target in (
            ("Number", "incident"), ("CI", "ci"), ("Service", "service"))
            if incident_fields.get(source)}
    approval = connection.require_approval or fields.get("Approval", "").casefold() not in {
        "", "no", "none", "not required", "false"}
    run = AutomationRun.objects.create(application=app, automation=automation, incident=incident,
        connection=connection, requested_by=user, mode="real", platform=platform,
        idempotency_key=key, status="pending_approval" if approval else "queued", request={
            "job": job, "inputs": inputs, "automation_digest": automation.digest,
            "incident_digest": incident.digest if incident else "",
            "connection_digest": connection_digest(connection), "requires_approval": approval},
        outcome=("Awaiting a different owner's approval." if approval
                 else "Queued for real execution."))
    audit(user, "automation.execution_queued", run.pk, app.product.portfolio.organization,
          details={"provider": connection.provider, "job": job, "approval_required": approval})
    return run


@transaction.atomic
def approve_execution(user, pk, run_id):
    app, _ = owner_access(user, pk)
    run = get_object_or_404(AutomationRun.objects.select_for_update(), application=app,
                           pk=run_id, mode="real", status="pending_approval")
    if user.pk == run.requested_by_id:
        raise PermissionDenied("Another application owner must approve this execution.")
    run.status, run.approved_by = "queued", user
    run.outcome = "Approved and queued for real execution."
    run.save(update_fields=["status", "approved_by", "outcome"])
    audit(user, "automation.execution_approved", run.pk, app.product.portfolio.organization)
    return run


def _check_run(run):
    app, _ = owner_access(run.requested_by, run.application_id)
    connection = AutomationConnection.objects.get(pk=run.connection_id, application=app)
    automation = automation_queryset(app).filter(pk=run.automation_id).first()
    if (not connection.enabled or connection.provider not in ADAPTERS or
        connection_digest(connection) != run.request["connection_digest"] or
        run.request["job"] not in connection.allowed_jobs or not automation or
        not verified(automation) or automation.digest != run.request["automation_digest"]):
        raise ValidationError("Connection or automation changed; execution was blocked.")
    if run.incident_id:
        incident = incident_queryset(app).filter(pk=run.incident_id).first()
        if (not incident or not verified(incident) or
                incident.digest != run.request["incident_digest"]):
            raise ValidationError("Incident evidence changed; execution was blocked.")
    if run.request["requires_approval"]:
        if not run.approved_by_id or run.approved_by_id == run.requested_by_id:
            raise PermissionDenied
        owner_access(run.approved_by, app.pk)
    return app, connection


def _finish(run, status, outcome, result=None, external_id=""):
    values = {"status": status, "outcome": outcome[:600], "last_checked_at": timezone.now()}
    if result is not None:
        values["result"] = {**run.result, **result}
    if external_id:
        values["external_job_id"] = external_id
    if status not in ACTIVE:
        values["finished_at"] = timezone.now()
    AutomationRun.objects.filter(pk=run.pk).update(**values)
    audit(run.requested_by, "automation.execution_status", run.pk,
          run.application.product.portfolio.organization, details={"status": status})


def process_next_execution():
    now = timezone.now()
    # A worker disappearing after a POST is ambiguous; never put it back in the queue.
    AutomationRun.objects.filter(mode="real", status="launching", started_at__lt=
        now - timedelta(minutes=10)).update(status="unknown", finished_at=now,
        outcome="Worker stopped during launch. Inspect the target before a new invocation.")
    run = AutomationRun.objects.filter(mode="real", status="running",
        last_checked_at__lt=now - timedelta(seconds=5)).order_by("last_checked_at").first()
    polling = run is not None
    if run is None:
        run = AutomationRun.objects.filter(mode="real", status="queued").order_by(
            "created_at").first()
        if run is None:
            return False
        if not AutomationRun.objects.filter(pk=run.pk, status="queued").update(
                status="launching", started_at=now, last_checked_at=now):
            return False
    else:
        # Lease a poll. A GET is repeatable, but no two workers need issue it together.
        if not AutomationRun.objects.filter(pk=run.pk, last_checked_at=run.last_checked_at).update(
                last_checked_at=now):
            return False
    from .tenancy import scoped

    # Bound to the run's application for its work (tenancy.py).
    with scoped(run.application_id):
        checked = False
        try:
            app, connection = _check_run(run)
            checked = True
            if polling and run.started_at < now - timedelta(minutes=30):
                _finish(run, "unknown", "Monitoring deadline reached. The external job may still "
                        "be running; inspect the controller.")
                return True
            adapter = ADAPTERS[connection.provider]
            response = (adapter.poll(app, connection, run.request["job"], run.external_job_id)
                        if polling else adapter.launch(app, connection, run.request["job"],
                                                       run.request["inputs"]))
            _finish(run, response.status,
                    "Verified local execution completed." if connection.provider == "local" else
                    f"AWX job {response.external_id}: {response.status}.", response.output,
                    response.external_id)
        except LaunchUnknown as exc:
            _finish(run, "unknown", str(exc))
        except FetchError:
            # Polling is read-only and can retry. Launch ambiguity is handled inside the adapter.
            _finish(run, "running", "Cannot read the controller status yet. Monitoring will retry.")
        except ValidationError as exc:
            status = "unknown" if polling and checked else "failed" if checked else "blocked"
            outcome = " ".join(exc.messages)
            if polling:
                outcome += " The external job was not canceled; inspect the controller."
            _finish(run, status, outcome)
        except (PermissionDenied, Http404, AutomationConnection.DoesNotExist):
            _finish(run, "blocked", "Execution blocked: access, approval, source, connection or "
                    "credentials changed. No new launch was made. "
                    "Existing remote jobs are not canceled.")
        except Exception:
            _finish(run, "unknown", "Execution outcome could not be recorded. Inspect the target "
                    "before invoking again; the job was not automatically retried.")
    return True


@login_required
@require_http_methods(["GET", "POST"])
def automations(request, pk):
    app, grant = owner_access(request.user, pk)
    form = ConnectionForm(request.POST if request.method == "POST" and
                          request.POST.get("action") == "connect_awx" else None)
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "enable_local":
                enable_local(request.user, pk)
                messages.success(request, "Local execution enabled. Two real runbooks registered.")
            elif action == "connect_awx":
                if form.is_valid():
                    values = form.cleaned_data
                    connection = AutomationConnection.objects.create(application=app,
                        created_by=request.user, provider="awx", **values)
                    audit(request.user, "automation.connection_created", connection.pk,
                          app.product.portfolio.organization)
                    messages.success(request, "AWX execution connection added. Set the separate "
                                     "AWX execute credential and import its job templates.")
                else:
                    return _render(request, app, grant, form)
            elif action == "invoke":
                queue_execution(request.user, pk, request.POST.get("automation"),
                    request.POST.get("connection"), request.POST.get("request_key"),
                    request.POST.get("incident") or None)
                messages.success(request, "Real execution requested. Results update automatically.")
            elif action == "approve":
                approve_execution(request.user, pk, _uuid(request.POST.get("run")))
                messages.success(request, "Execution approved.")
            elif action == "disable_connection":
                connection = get_object_or_404(AutomationConnection, application=app,
                                               pk=_uuid(request.POST.get("connection")))
                connection.enabled = False
                connection.save(update_fields=["enabled"])
                audit(request.user, "automation.connection_disabled", connection.pk,
                      app.product.portfolio.organization)
                messages.success(request, "Connection disabled. Queued work is blocked; already "
                                 "running external jobs are not canceled.")
            else:
                raise ValidationError("Choose an available automation action.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        except IntegrityError:
            messages.error(request, "An execution is already active. Wait for it to finish.")
        return redirect("serviceops-automations", pk=pk)
    return _render(request, app, grant, form)


def _render(request, app, grant, form):
    connections = list(AutomationConnection.objects.filter(application=app))
    entries = available_jobs(app, connections)
    runs = list(AutomationRun.objects.filter(application=app, mode="real").select_related(
        "automation", "connection", "requested_by", "approved_by")[:50])
    return render(request, "serviceops_automations.html", {"application": app, "grant": grant,
        "view": "automations", "form": form, "connections": connections, "entries": entries,
        "runs": runs, "active": any(run.status in ACTIVE for run in runs),
        "watching": any(run.status in ACTIVE | {"pending_approval"} for run in runs),
        "local_enabled": any(c.provider == "local" and c.enabled for c in connections),
        "incidents": incident_queryset(app).order_by("-created_at")[:100]})


def available_jobs(app, connections):
    entries = []
    for entry in automation_queryset(app).order_by("title")[:200]:
        if verified(entry):
            fields, _ = fields_and_description(entry)
            choices = [connection for connection in connections if connection.enabled and
                fields.get("Automation ID") in connection.allowed_jobs and
                fields.get("Platform") == {"local": "Digital Brain Local", "awx": "AWX"}.get(
                    connection.provider) and automation_usable(fields)]
            if choices:
                entries.append({"entry": entry, "fields": fields, "connections": choices,
                                "request_key": uuid.uuid4()})
    return entries


def run_payload(run):
    return {"id": str(run.pk), "mode": run.mode, "status": run.status,
            "automation_id": str(run.automation_id), "connection_id": str(run.connection_id),
            "external_job_id": run.external_job_id, "outcome": run.outcome,
            "result": run.result, "started_at": run.started_at, "finished_at": run.finished_at}


@csrf_exempt
@require_http_methods(["GET", "POST"])
def execution_api(request, reference, run_id=None):
    try:
        user, _, app = authenticate(request, reference)
        owner_access(user, app.pk)
        if request.method == "GET" and run_id:
            run = get_object_or_404(AutomationRun, application=app, mode="real", pk=run_id)
            return JsonResponse(run_payload(run))
        if request.method == "GET":
            entries = available_jobs(app, list(AutomationConnection.objects.filter(
                application=app)))
            return JsonResponse({"jobs": [{"automation_id": str(item["entry"].pk),
                "connection_id": str(connection.pk), "title": item["entry"].title,
                "provider": connection.provider, "job": item["fields"].get("Automation ID"),
                "requires_approval": connection.require_approval or
                item["fields"].get("Approval", "").casefold() not in {
                    "", "no", "none", "not required", "false"}}
                for item in entries for connection in item["connections"]]})
        if request.method != "POST" or run_id:
            return JsonResponse({"error": "Use POST to invoke, GET with a run ID to inspect."},
                                status=405)
        if len(request.body) > 8192:
            raise ApiError("Request body is too large.", status=413)
        try:
            data = json.loads(request.body)
        except ValueError:
            raise ApiError("Provide a JSON object.") from None
        if not isinstance(data, dict):
            raise ApiError("Provide a JSON object.")
        run = queue_execution(user, app.pk, data.get("automation_id"), data.get("connection_id"),
                              data.get("request_key"), data.get("incident_id"))
        return JsonResponse(run_payload(run), status=202)
    except ApiError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}},
                            status=exc.status)
    except PermissionDenied:
        return JsonResponse({"error": "An authorized application owner is required."}, status=403)
    except Http404:
        return JsonResponse({"error": "Not found."}, status=404)
    except (ValidationError, IntegrityError):
        return JsonResponse({"error": "Invalid, changed, unapproved or already active invocation."},
                            status=409)
