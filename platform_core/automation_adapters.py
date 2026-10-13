"""Execution adapters: bound local runbooks and an optional external AWX controller.

Adapters receive an authorized application, immutable inputs and a connection.
They never accept shell text. Launch and poll are separate, so external jobs do
not hold a web request open and a poll can never repeat the launch.
"""

import json
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from .fetching import FetchError, fetch
from .models import IncidentProfile, KnowledgeEntry, OpsEdge, OpsNode
from .secrets import application_secret

LOCAL_JOBS = {
    "rebuild_ops_graph": ("Rebuild ServiceOps graph", "Rebuilds application-scoped machine "
                         "relationships and verifies the resulting fingerprint and counts."),
    "reprofile_incidents": ("Re-profile incidents", "Refreshes verified incident profiles, "
                            "rebuilds their graph and verifies each stored source digest."),
}


@dataclass
class JobResult:
    status: str
    external_id: str = ""
    output: dict = field(default_factory=dict)


class LaunchUnknown(Exception):
    """A remote POST may have taken effect. Never automatically submit it again."""


class LocalAdapter:
    @staticmethod
    def launch(app, connection, job, inputs):
        from .ops_graph import rebuild, records_fingerprint
        from .serviceops import incident_queryset, verified
        from .serviceops_triage import profile

        if job not in LOCAL_JOBS:
            raise ValidationError("This local runbook is not registered.")
        # Actual changes, in one transaction; failed postconditions roll them back.
        with transaction.atomic():
            count = 0
            valid = []
            if job == "reprofile_incidents":
                for entry in incident_queryset(app).iterator(chunk_size=200):
                    if verified(entry):
                        profile(entry)
                        valid.append((entry.pk, entry.digest))
                        count += 1
            state = rebuild(app)
            nodes = OpsNode.objects.filter(application=app).count()
            edges = OpsEdge.objects.filter(application=app).count()
            matched = state.fingerprint == records_fingerprint(app)
            if not matched or nodes != state.node_count or edges != state.edge_count:
                raise ValidationError("Graph verification failed; changes were rolled back.")
            if valid:
                expected = dict(valid)
                fresh = list(KnowledgeEntry.objects.filter(application=app,
                    pk__in=list(expected)).answerable())
                if len(fresh) != len(valid) or any(
                        not verified(entry) or entry.digest != expected[entry.pk]
                        for entry in fresh):
                    raise ValidationError("Sources changed during execution; "
                                          "changes were rolled back.")
                profiles = dict(IncidentProfile.objects.filter(application=app,
                    entry_id__in=[pk for pk, _ in valid]).values_list("entry_id", "source_digest"))
                if any(profiles.get(pk) != digest for pk, digest in valid):
                    raise ValidationError("Profile verification failed; changes were rolled back.")
            return JobResult("succeeded", output={"nodes": nodes, "edges": edges,
                "profiles_refreshed": count, "verification": "passed",
                "source_fingerprint": state.fingerprint,
                "effects": "Machine-built graph replaced; human-confirmed edges preserved."})

    @staticmethod
    def poll(app, connection, job, external_id):
        raise ValidationError("Local runbooks finish during their launch step.")


class AWXAdapter:
    @staticmethod
    def request(app, connection, path, *, payload=None):
        credential = application_secret(app, "awx_execute")
        if not credential:
            raise ValidationError("Set the application's AWX execute credential first.")
        body, _, _, _ = fetch(connection.base_url + path, headers={
            "Authorization": f"Bearer {credential}", "Accept": "application/json",
            "Content-Type": "application/json"}, method="POST" if payload is not None else "GET",
            body=json.dumps(payload).encode() if payload is not None else None)
        try:
            data = json.loads(body)
        except ValueError:
            raise ValidationError("AWX returned an invalid job response.") from None
        if not isinstance(data, dict):
            raise ValidationError("AWX returned an invalid job response.")
        return data

    def launch(self, app, connection, job, inputs):
        # Refuse before POST if credentials are absent; any ambiguous POST stays unknown.
        if not application_secret(app, "awx_execute"):
            raise ValidationError("Set the application's AWX execute credential first.")
        try:
            data = self.request(app, connection, f"/api/v2/job_templates/{job}/launch/",
                                payload={"extra_vars": inputs})
            ident = data.get("job") or data.get("id")
            if not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0:
                raise LaunchUnknown("AWX did not return a job ID; inspect the controller.")
        except FetchError as exc:
            if exc.status and 400 <= exc.status < 500:
                raise ValidationError("AWX refused the launch. Check Execute permissions and "
                                      "template inputs.") from None
            raise LaunchUnknown("Launch response was lost or ambiguous. Inspect AWX before "
                                "submitting a new request; no automatic retry was made.") from None
        except ValidationError:
            raise LaunchUnknown("AWX returned an invalid launch response. Inspect the controller "
                                "before submitting a new request.") from None
        if data.get("ignored_fields"):
            # A job already exists; keep tracking it, but record that inputs were ignored.
            return JobResult("running", str(ident), {"warning": "AWX ignored some requested "
                             "inputs. Configure prompt-on-launch before using this template."})
        return JobResult("running", str(ident))

    def poll(self, app, connection, job, external_id):
        if not external_id.isdecimal():
            raise ValidationError("Invalid remote job ID.")
        data = self.request(app, connection, f"/api/v2/jobs/{external_id}/")
        if str(data.get("job_template")) != job:
            raise ValidationError("The remote job does not belong to the allowed template.")
        state = data.get("status")
        output = {"remote_status": state, "verification": "controller-job-status",
                  "scope": "Controller success does not establish service recovery."}
        if state == "successful":
            return JobResult("succeeded", external_id, output)
        if state in {"failed", "error", "canceled"}:
            return JobResult("failed", external_id, output)
        if state not in {"new", "pending", "waiting", "running"}:
            raise ValidationError("AWX returned an unknown execution status.")
        return JobResult("running", external_id, output)


ADAPTERS = {"local": LocalAdapter(), "awx": AWXAdapter()}
