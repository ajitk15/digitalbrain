"""Owner-only automation of a closed ServiceOps suite, outside the live database."""

import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .models import Application, ServiceOpsTestRun
from .services import audit
from .workbench import access

SUITES = {
    "e2e": {"label": "Browser journey", "description":
            "Real Chrome: incident creation → triage → assessment → simulation, plus "
            "verified local maintenance execution.",
            "labels": ["platform_core.tests.test_serviceops_e2e",
                       "platform_core.tests.test_automation_execution.LocalExecutionBrowserTests"]},
    "regression": {"label": "Full ServiceOps regression", "description":
                   "Browser journey plus connector mapping, graph, permissions and failure paths.",
                   "labels": ["platform_core.tests.test_serviceops_e2e",
                              "platform_core.tests.test_automation_execution",
                              "platform_core.tests.test_serviceops",
                              "platform_core.tests.test_serviceops_knowledge"]},
}
TIMEOUT = 600


def owner_access(user, pk):
    app, grant = access(user, pk, "service_ops")
    access(user, pk, "knowledge")
    if grant.role != "owner":
        raise PermissionDenied
    return app, grant


def queue_test(user, pk, suite):
    app, _ = owner_access(user, pk)
    if not settings.SERVICEOPS_TEST_AUTOMATION:
        raise PermissionDenied("Test automation is available on development instances only.")
    if suite not in SUITES:
        raise ValueError("Choose one of the available test suites.")
    with transaction.atomic():
        Application.objects.select_for_update().get(pk=app.pk)
        if ServiceOpsTestRun.objects.filter(application=app, created_at__gte=
                                           timezone.now() - timedelta(minutes=1)).exists():
            raise ValueError("Wait one minute before starting another run.")
        run = ServiceOpsTestRun.objects.create(application=app, requested_by=user, suite=suite)
        audit(user, "serviceops.test_queued", run.pk, app.product.portfolio.organization,
              details={"suite": suite})
        return run


@login_required
@require_http_methods(["GET", "POST"])
def testing(request, pk):
    app, grant = owner_access(request.user, pk)
    if request.method == "POST":
        try:
            queue_test(request.user, pk, request.POST.get("suite", ""))
        except (ValueError, IntegrityError) as exc:
            messages.error(request, str(exc) if isinstance(exc, ValueError)
                           else "A run is already queued or running.")
        else:
            messages.success(request, "Test run queued. The worker will run it in isolation.")
        return redirect("serviceops-testing", pk=pk)
    runs = list(ServiceOpsTestRun.objects.filter(application=app).select_related(
        "requested_by")[:30])
    latest = runs[0] if runs else None
    counts = {status: sum(row.get("status") == status for row in latest.results)
              if latest else 0 for status in ("passed", "failed", "error", "skipped")}
    return render(request, "serviceops_testing.html", {
        "application": app, "grant": grant, "view": "testing", "suites": SUITES,
        "runs": runs, "latest": latest, "counts": counts,
        "active": any(run.status in {"queued", "running"} for run in runs),
        "enabled": settings.SERVICEOPS_TEST_AUTOMATION,
    })


@login_required
@require_GET
def report(request, pk, run_id):
    app, _ = owner_access(request.user, pk)
    run = get_object_or_404(ServiceOpsTestRun, application=app, pk=run_id)
    response = JsonResponse({"run": str(run.pk), "suite": run.suite, "status": run.status,
                             "summary": run.summary, "results": run.results,
                             "scope": "Isolated database, real browser, actual local maintenance, "
                                      "mocked AI and external automation simulation. "
                                      "No live provider or external remediation execution."})
    response["Content-Disposition"] = f'attachment; filename="serviceops-test-{run.pk}.json"'
    return response


def execute_test(run):
    """Fixed argv and fresh settings; never accept a command or target from the browser."""
    try:
        owner_access(run.requested_by, run.application_id)
        if not settings.SERVICEOPS_TEST_AUTOMATION:
            raise PermissionDenied
    except (Http404, PermissionDenied):
        return "blocked", [], "Test execution disabled or owner access revoked."
    with tempfile.TemporaryDirectory(prefix="serviceops-test-") as directory:
        receipt = Path(directory) / "report.json"
        allowed_env = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
                       "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
        env.update(SERVICEOPS_TEST_DIRECTORY=directory, SERVICEOPS_TEST_REPORT=str(receipt),
                   DIGITAL_BRAIN_REQUIRE_BROWSER="1", PYTHONIOENCODING="utf-8")
        command = [sys.executable, str(settings.BASE_DIR / "manage.py"), "test",
                   *SUITES[run.suite]["labels"], "--noinput", "--verbosity=1",
                   "--settings=digitalbrain.serviceops_test_settings"]
        try:
            result = subprocess.run(command, cwd=settings.BASE_DIR, env=env, timeout=TIMEOUT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    check=False, creationflags=getattr(subprocess,
                                    "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return "failed", [], "The suite exceeded its ten-minute limit."
        if not receipt.is_file():
            return "blocked", [], "Suite could not start. Check Python, Playwright and Chrome."
        data = json.loads(receipt.read_text(encoding="utf-8"))
        rows = data["tests"]
        skipped = any(row["status"] == "skipped" for row in rows)
        success = result.returncode == 0 and data["successful"] and rows and not skipped
        return ("passed" if success else "failed"), rows, (
            f"{data['count']} tests executed. "
            "Isolated browser/application checks; AI mocked and automation simulated."
            + (" Skipped coverage prevents a pass." if skipped else ""))


def process_next_test():
    """Atomic claim; recover interrupted workers rather than leave an endless spinner."""
    now = timezone.now()
    ServiceOpsTestRun.objects.filter(status="running", started_at__lt=
                                    now - timedelta(seconds=TIMEOUT + 120)).update(
        status="blocked", finished_at=now, summary="Test worker stopped. Start a new run.")
    if ServiceOpsTestRun.objects.filter(status="running").exists():
        return False
    run = ServiceOpsTestRun.objects.filter(status="queued").order_by("created_at").first()
    if run is None:
        return False
    try:
        claimed = ServiceOpsTestRun.objects.filter(pk=run.pk, status="queued").update(
            status="running", started_at=now)
    except IntegrityError:
        return False
    if not claimed:
        return False
    from .tenancy import scoped

    try:
        with scoped(run.application_id):
            status, rows, summary = execute_test(run)
    except Exception:
        status, rows, summary = (
            "blocked", [], "Execution unavailable. Check the worker setup/access.")
    ServiceOpsTestRun.objects.filter(pk=run.pk, status="running").update(
        status=status, results=rows, summary=summary, finished_at=timezone.now())
    return True
