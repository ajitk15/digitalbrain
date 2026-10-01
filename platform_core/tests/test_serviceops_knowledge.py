"""ServiceOps' knowledge graph: KB articles and automations as first-class nodes.

What these pin:

* Every automation source - AWX, Rundeck, Azure Automation, ServiceNow flows and
  a team's own list - reduces to one header, so the graph links them the same way.
* A KB article enters the graph when ServiceNow says Published and in date, and
  leaves it when ServiceNow retires it: ServiceNow's review is the gate.
* Rules, not a model, join them: covers, cites, targets, remediates, references,
  resolved by. Triage's evidence and the incident page read them.
* An automation is never executed. "Simulate run" records what would be sent.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from platform_core.connector_kinds import (
    automation_list_records,
    automation_meta,
    awx_records,
    azure_automation_records,
    rundeck_records,
    servicenow_records,
)
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    AutomationRun,
    KnowledgeEntry,
    OpsEdge,
    OpsNode,
)
from platform_core.ops_graph import neighbourhood, rebuild
from platform_core.workbench import add_knowledge

from . import test_documents

SN = "https://acme.service-now.com"


class AutomationSourceTests(SimpleTestCase):
    """Each source becomes the same automation header."""

    def test_metadata_comes_from_tags_and_from_description_lines(self):
        meta = automation_meta(
            ["ci:export-worker", "symptom:timeout", ("service", "CarePath"), "unrelated"],
            "Risk: low\nSymptom: 504\nApproval: required\nNot: a field",
        )
        self.assertEqual(meta["CI"], "export-worker")
        self.assertEqual(meta["Service"], "CarePath")
        self.assertEqual(meta["Symptoms"], "timeout, 504")
        self.assertEqual(meta["Risk"], "low")
        self.assertEqual(meta["Approval"], "required")

    def test_a_value_cannot_forge_another_header_line(self):
        meta = automation_meta(["ci:api\nType: Incident"])
        self.assertNotIn("\n", meta["CI"])

    def test_awx_job_templates(self):
        payload = {
            "results": [
                {
                    "id": 42,
                    "name": "Restart export worker",
                    "description": "Restarts the FHIR export worker.",
                    "playbook": "restart_export.yml",
                    "ask_variables_on_launch": True,
                    "summary_fields": {
                        "labels": {"results": [{"name": "ci:carepath-api-green"}]},
                        "last_job": {"status": "successful", "finished": "2026-09-30T10:00:00Z"},
                    },
                }
            ]
        }
        with patch("platform_core.connector_kinds.api_json", return_value=payload) as api:
            (record,) = awx_records({"base_url": "https://awx.example.com"}, "token")
        self.assertIn("/api/v2/job_templates/", api.call_args.args[0])
        self.assertEqual(api.call_args.args[1]["Authorization"], "Bearer token")
        self.assertEqual(record.title, "Restart export worker")
        for line in (
            "Type: Automation",
            "Platform: AWX",
            "Automation ID: 42",
            "CI: carepath-api-green",
            "Inputs: extra variables",
            "Last status: successful",
        ):
            self.assertIn(line, record.body)
        self.assertTrue(record.url.startswith("https://awx.example.com/#/templates/job_template/42"))

    def test_rundeck_jobs(self):
        jobs = [{"id": "a1b2", "name": "flush-cache", "group": "ops", "description": "CI: cache-1"}]
        with patch("platform_core.connector_kinds.api_json", return_value=jobs) as api:
            (record,) = rundeck_records(
                {"base_url": "https://rd.example.com", "project": "carepath"}, "tok"
            )
        self.assertIn("/api/41/project/carepath/jobs", api.call_args.args[0])
        self.assertEqual(api.call_args.args[1]["X-Rundeck-Auth-Token"], "tok")
        self.assertEqual(record.title, "ops/flush-cache")
        self.assertIn("Platform: Rundeck", record.body)
        self.assertIn("CI: cache-1", record.body)

    def test_azure_automation_runbooks(self):
        config = {
            "tenant": "contoso.onmicrosoft.com",
            "client_id": "abc",
            "subscription": "sub-1",
            "resource_group": "ops-rg",
            "account": "ops-auto",
        }
        runbooks = {
            "value": [
                {
                    "name": "Restart-AppPool",
                    "tags": {"ci": "iis-01", "risk": "medium"},
                    "properties": {"description": "Recycles the pool.", "state": "Published"},
                }
            ]
        }
        with (
            patch("platform_core.connector_kinds.azure_token", return_value="t"),
            patch("platform_core.connector_kinds.api_json", return_value=runbooks) as api,
        ):
            (record,) = azure_automation_records(config, "secret")
        self.assertIn("automationAccounts/ops-auto/runbooks", api.call_args.args[0])
        self.assertIn("Platform: Azure Automation", record.body)
        self.assertIn("CI: iis-01", record.body)
        self.assertIn("Risk: medium", record.body)
        self.assertIn("State: Published", record.body)

    def test_a_pasted_list_needs_a_name_column_and_skips_blank_rows(self):
        content = (
            "name,id,platform,ci,symptoms,risk\n"
            "Restart export worker,rew,AWX,carepath-api-green,timeout,low\n"
            ",,,,,\n"
        )
        (record,) = automation_list_records({"list_name": "Ops", "content": content}, "")
        self.assertEqual(record.external_id, "rew")
        self.assertIn("Symptoms: timeout", record.body)
        self.assertTrue(record.url.startswith("automation-list:Ops/"))
        with self.assertRaises(ValidationError):
            automation_list_records({"list_name": "Ops", "content": "id,ci\n1,x\n"}, "")

    def test_servicenow_flows_are_automations_and_an_inactive_one_says_so(self):
        payload = {
            "result": [
                {"sys_id": "f1", "name": "Restart export", "description": "CI: api-1",
                 "active": "true", "status": "Published"},
                {"sys_id": "f2", "name": "Old flow", "description": "", "active": "false"},
            ]
        }
        config = {"base_url": SN, "identity": "u", "table": "sys_hub_flow"}
        with patch("platform_core.connector_kinds.api_json", return_value=payload):
            live, old = servicenow_records(config, "p")
        self.assertEqual(live.title, "Restart export")
        self.assertIn("Platform: ServiceNow Flow Designer", live.body)
        self.assertIn("State: Inactive", old.body)

    def test_servicenow_knowledge_articles_carry_their_workflow_state(self):
        payload = {
            "result": [
                {
                    "sys_id": "k1",
                    "number": "KB0010023",
                    "short_description": "Export times out on large panels",
                    "text": "<p>Raise the <b>ingress</b> timeout &amp; page the bundle.</p>",
                    "workflow_state": "Published",
                    "valid_to": "2100-01-01",
                    "cmdb_ci": {"display_value": "carepath-api-green"},
                    "kb_category": {"display_value": "FHIR"},
                }
            ]
        }
        config = {"base_url": SN, "identity": "u", "table": "kb_knowledge"}
        with patch("platform_core.connector_kinds.api_json", return_value=payload) as api:
            (record,) = servicenow_records(config, "p")
        self.assertIn("workflow_state", api.call_args.args[0])
        self.assertIn("Type: Knowledge article", record.body)
        self.assertIn("State: Published", record.body)
        self.assertIn("CI: carepath-api-green", record.body)
        self.assertIn("Raise the ingress timeout & page the bundle.", record.body)
        self.assertNotIn("<p>", record.body)


class KnowledgeGraphFixtures:
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def store(self, record, app=None):
        return add_knowledge(
            self.owner,
            (app or self.app).pk,
            record.title,
            f"{record.title}\n\n{record.body}",
            source=record.url,
        )

    def incident(self, number, title, *, resolved=False, notes="", ci="carepath-api-green"):
        header = [
            "Type: Incident",
            f"Number: {number}",
            f"State: {'Resolved' if resolved else 'New'}",
            "Service: CarePath",
            f"CI: {ci}",
            "Opened: 2026-09-20T10:00:00Z",
        ]
        if resolved:
            header += ["Resolved: 2026-09-20T12:00:00Z", f"Close notes: {notes or 'Fixed.'}"]
        content = f"{title}\n\n" + "\n".join(header) + "\n\nExport returns 504 gateway timeout."
        return add_knowledge(
            self.owner,
            self.app.pk,
            title,
            content,
            source=f"{SN}/nav_to.do?uri=incident.do%3Fsys_id%3D{number}",
        )

    def article(self, number, *, state="Published", valid_to="2100-01-01", text="", ci=""):
        payload = {
            "result": [
                {
                    "sys_id": number.lower(),
                    "number": number,
                    "short_description": f"How to fix {number}",
                    "text": text or "<p>Raise the ingress timeout.</p>",
                    "workflow_state": state,
                    "valid_to": valid_to,
                    "cmdb_ci": ci,
                }
            ]
        }
        config = {"base_url": SN, "identity": "u", "table": "kb_knowledge"}
        with patch("platform_core.connector_kinds.api_json", return_value=payload):
            (record,) = servicenow_records(config, "p")
        return self.store(record)

    def automation(self, name, ident, csv_extra="carepath-api-green,timeout,low,"):
        content = f"name,id,platform,ci,symptoms,risk,approval\n{name},{ident},AWX,{csv_extra}\n"
        (record,) = automation_list_records({"list_name": "Ops", "content": content}, "")
        return self.store(record)

    def related(self, relation):
        return {
            (edge.source.kind, edge.target.kind)
            for edge in OpsEdge.objects.filter(application=self.app, relation=relation)
        }


class KnowledgeGraphTests(KnowledgeGraphFixtures, TestCase):
    def test_a_published_article_covers_its_component_and_is_cited_by_number(self):
        article = self.article("KB0010023", ci="carepath-api-green")
        self.incident("INC1", "Export 504", resolved=True, notes="Applied KB0010023.")
        rebuild(self.app)
        self.assertTrue(OpsNode.objects.filter(application=self.app, kind="kb", entry=article))
        self.assertIn(("component", "kb"), self.related("covers"))
        self.assertIn(("incident", "kb"), self.related("cites"))

    def test_servicenow_state_is_the_gate(self):
        """Draft, retired and expired articles stay out; a published one is in."""
        self.incident("INC1", "Export 504")
        self.article("KB1000001", state="Draft", ci="carepath-api-green")
        self.article("KB1000002", state="Retired", ci="carepath-api-green")
        self.article("KB1000003", valid_to="2001-01-01", ci="carepath-api-green")
        live = self.article("KB1000004", ci="carepath-api-green")
        rebuild(self.app)
        kept = OpsNode.objects.filter(application=self.app, kind="kb")
        self.assertEqual([node.entry_id for node in kept], [live.pk])

    def test_retiring_an_article_in_servicenow_removes_it_on_the_next_sync(self):
        self.incident("INC1", "Export 504")
        article = self.article("KB1000004", ci="carepath-api-green")
        rebuild(self.app)
        self.assertTrue(OpsNode.objects.filter(application=self.app, kind="kb").exists())
        # A sync supersedes the old revision with the retired one.
        article.active = False
        article.save(update_fields=["active"])
        self.article("KB1000004", state="Retired", ci="carepath-api-green")
        rebuild(self.app)
        self.assertFalse(OpsNode.objects.filter(application=self.app, kind="kb").exists())

    def test_an_automation_targets_and_remediates_and_resolved_a_past_incident(self):
        self.incident("INC1", "Export 504 earlier", resolved=True,
                      notes="Ran Restart export worker; export completed.")
        self.incident("INC2", "Export 504 again")
        self.article("KB0010023", ci="carepath-api-green",
                     text="<p>If it recurs, run Restart export worker.</p>")
        self.automation("Restart export worker", "rew")
        rebuild(self.app)
        self.assertIn(("component", "automation"), self.related("targets"))
        self.assertIn(("symptom", "automation"), self.related("remediates"))
        self.assertIn(("incident", "automation"), self.related("resolved_by"))
        self.assertIn(("kb", "automation"), self.related("references"))

    def test_an_inactive_automation_is_not_suggested(self):
        self.incident("INC2", "Export 504 again")
        payload = {"result": [{"sys_id": "f2", "name": "Old flow restart",
                               "description": "CI: carepath-api-green", "active": "false"}]}
        config = {"base_url": SN, "identity": "u", "table": "sys_hub_flow"}
        with patch("platform_core.connector_kinds.api_json", return_value=payload):
            (record,) = servicenow_records(config, "p")
        self.store(record)
        rebuild(self.app)
        self.assertFalse(OpsNode.objects.filter(application=self.app, kind="automation").exists())

    def test_the_walk_ranks_what_fixed_a_similar_incident_first(self):
        self.incident("INC1", "Export 504 gateway timeout", resolved=True,
                      notes="Ran Restart export worker. See KB0010023.")
        current = self.incident("INC2", "Export 504 gateway timeout again")
        self.article("KB0010023")
        self.automation("Restart export worker", "rew")
        self.automation("Scale export pool", "sep")
        walk = neighbourhood(self.app, current)
        names = [row["entry"].title for row in walk["automations"]]
        self.assertEqual(names[0], "Restart export worker")
        self.assertIn("resolved INC1 before", walk["automations"][0]["reasons"])
        self.assertIn("KB0010023", [row["fields"]["Number"] for row in walk["articles"]])

    def test_triage_evidence_includes_articles_and_automations(self):
        from platform_core.serviceops_triage import evidence_pack

        self.incident("INC1", "Export 504 gateway timeout", resolved=True,
                      notes="Ran Restart export worker. See KB0010023.")
        current = self.incident("INC2", "Export 504 gateway timeout again")
        self.article("KB0010023")
        self.automation("Restart export worker", "rew")
        pack, _ = evidence_pack(self.app, current)
        kinds = {row["kind"] for row in pack}
        self.assertIn("kb_article", kinds)
        self.assertIn("automation", kinds)
        for row in pack:
            entry = KnowledgeEntry.objects.get(pk=row["id"])
            self.assertIn(row["excerpt"], entry.content, "every excerpt must be quotable")

    def test_another_applications_records_never_join(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        current = self.incident("INC2", "Export 504 again")
        content = "name,id,platform,ci\nRestart export worker,rew,AWX,carepath-api-green\n"
        (record,) = automation_list_records({"list_name": "Ops", "content": content}, "")
        self.store(record, app=self.other)
        walk = neighbourhood(self.app, current)
        self.assertEqual(walk["automations"], [])


class SimulatedRunTests(KnowledgeGraphFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.current = self.incident("INC2", "Export 504 again")
        self.tool = self.automation("Restart export worker", "rew", "carepath-api-green,timeout,"
                                    "low,required by a second person")
        self.url = reverse("serviceops-automation-simulate", args=[self.app.pk, self.tool.pk])

    def test_a_simulated_run_records_what_would_be_sent_and_sends_nothing(self):
        with patch("platform_core.fetching.fetch") as fetch:
            response = self.client.post(self.url, {"incident": str(self.current.pk)})
        fetch.assert_not_called()
        self.assertEqual(response.status_code, 302)
        run = AutomationRun.objects.get()
        self.assertEqual(run.mode, "simulated")
        self.assertEqual(run.incident, self.current)
        self.assertEqual(run.request["body"]["inputs"]["incident"], "INC2")
        self.assertIn("nothing was sent", run.outcome)
        self.assertIn("Approval: required by a second person", run.outcome)
        self.assertTrue(AuditEvent.objects.filter(action="automation.simulated").exists())

    def test_an_awx_run_would_launch_its_job_template(self):
        from platform_core.serviceops import launch_request

        request = launch_request(
            {"Platform": "AWX", "Automation ID": "42", "Endpoint": "https://awx.example.com"},
            {"Number": "INC2", "CI": "api-1"},
        )
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["address"], "https://awx.example.com/api/v2/job_templates/42/launch/"
        )
        self.assertEqual(request["body"], {"extra_vars": {"incident": "INC2", "ci": "api-1"}})

    def test_a_viewer_cannot_simulate(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(self.url, {"incident": str(self.current.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AutomationRun.objects.exists())

    def test_an_incident_is_required_and_must_be_this_applications(self):
        self.assertEqual(self.client.post(self.url, {"incident": "nope"}).status_code, 404)
        other = reverse("serviceops-automation-simulate", args=[self.other.pk, self.tool.pk])
        self.assertEqual(
            self.client.post(other, {"incident": str(self.current.pk)}).status_code, 404
        )
        self.assertFalse(AutomationRun.objects.exists())

    def test_the_incident_page_offers_it_and_lists_what_was_simulated(self):
        page = self.client.get(reverse("serviceops", args=[self.app.pk]),
                               {"incident": str(self.current.pk)})
        self.assertContains(page, "Automations that may help")
        self.assertContains(page, "Simulated runs only")
        self.assertContains(page, self.url)
        self.client.post(self.url, {"incident": str(self.current.pk)})
        page = self.client.get(reverse("serviceops", args=[self.app.pk]),
                               {"incident": str(self.current.pk)})
        self.assertContains(page, "Simulated runs for this incident (1)")
