"""The operations graph is built by rules, per application, and kept current."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import ApplicationGrant, OperationsGraph, OpsEdge, OpsNode
from platform_core.ops_graph import clear, ensure_current, rebuild
from platform_core.serviceops import create_manual_incident, precedent_rows, related_open_rows
from platform_core.workbench import add_knowledge

from . import test_documents

INCIDENT_SOURCE = "https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D{}"
CHANGE_SOURCE = "https://acme.service-now.com/nav_to.do?uri=change_request.do%3Fsys_id%3D{}"


class Fixtures:
    """Records for both test classes: a few incidents, and changes on one component."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        # Records in the other application need someone allowed to add them.
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")

    def record(self, title, header, body, source, app=None):
        lines = "\n".join(f"{key}: {value}" for key, value in header.items())
        return add_knowledge(
            self.owner, (app or self.app).pk, title, f"{title}\n\n{lines}\n\n{body}", source=source
        )

    def incident(self, number, title, body, *, resolved=False, ci="api-green", app=None, **extra):
        header = {"Type": "Incident", "Number": number, "State": "Resolved" if resolved else "New"}
        header |= {"Service": "CarePath", "CI": ci, "Opened": "2026-09-20T10:00:00Z"}
        if resolved:
            header |= {"Resolved": "2026-09-20T12:00:00Z", "Close notes": f"Fixed {number}."}
        header |= extra
        return self.record(title, header, body, INCIDENT_SOURCE.format(number), app)

    def change(self, number, start, ci="api-green"):
        header = {"Type": "Change", "Number": number, "State": "Closed", "CI": ci, "Start": start}
        return self.record(
            f"Deploy {number}", header, "Rolled out a new release.", CHANGE_SOURCE.format(number)
        )

    def keys(self, relation):
        return {
            (edge.source.label.split(" · ")[0], edge.target.label.split(" · ")[0])
            for edge in OpsEdge.objects.filter(application=self.app, relation=relation)
        }

    def scenario(self):
        current = self.incident("INC9", "Export times out for clinic C", "504 Gateway Timeout.")
        self.incident("INC2", "Export timed out", "Bulk export 504 gateway timeout.", resolved=True)
        self.incident("INC10", "Clinic C export timing out", "Clinic C export 504 Gateway Timeout.")
        self.change("CHG1", "2026-09-20T07:48:00Z")
        self.change("CHG2", "2026-09-10T07:48:00Z")
        return current


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OperationsGraphTests(Fixtures, TestCase):
    """Phase 1: the graph is built by rules, per application, and kept current."""

    def test_rules_join_incidents_components_changes_and_precedents(self):
        self.scenario()
        state = rebuild(self.app)
        on = {edge.target.label for edge in OpsEdge.objects.filter(relation="on")}
        self.assertEqual(on, {"api-green"})
        self.assertEqual(OpsNode.objects.filter(application=self.app, kind="component").count(), 1)
        self.assertIn(("CHG1", "INC9"), self.keys("before"))
        self.assertNotIn(("CHG2", "INC9"), self.keys("before"))
        before = OpsEdge.objects.get(
            relation="before", source__label__startswith="CHG1 ", target__label__startswith="INC9 "
        )
        self.assertEqual(before.weight, 2.2)
        self.assertIn(("INC9", "INC2"), self.keys("similar"))
        self.assertIn(("INC9", "INC10"), self.keys("duplicate"))
        self.assertNotIn(("INC9", "INC2"), self.keys("duplicate"))
        symptoms = set(OpsNode.objects.filter(kind="symptom").values_list("label", flat=True))
        self.assertIn("timeout", symptoms)
        self.assertEqual(state.node_count, OpsNode.objects.filter(application=self.app).count())

    def test_graph_and_page_share_one_definition(self):
        current = self.scenario()
        rebuild(self.app)
        node = OpsNode.objects.get(key=f"incident:{current.pk}")
        similar = {edge.target.entry_id for edge in node.outgoing.filter(relation="similar")}
        duplicate = {edge.target.entry_id for edge in node.outgoing.filter(relation="duplicate")}
        self.assertEqual(similar, {row["entry"].pk for row in precedent_rows(self.app, current)})
        self.assertEqual(
            duplicate, {row["entry"].pk for row in related_open_rows(self.app, current)}
        )

    def test_another_application_never_appears(self):
        self.scenario()
        self.incident(
            "INC77", "Export times out", "504 Gateway Timeout.", resolved=True, app=self.other
        )
        rebuild(self.app)
        rebuild(self.other)
        for edge in OpsEdge.objects.filter(application=self.app).select_related("source", "target"):
            self.assertEqual(edge.source.application_id, self.app.pk)
            self.assertEqual(edge.target.application_id, self.app.pk)
        labels = OpsNode.objects.filter(application=self.app).values_list("label", flat=True)
        self.assertFalse(any(label.startswith("INC77") for label in labels))
        # The same component name in two applications is two nodes, never one.
        self.assertEqual(OpsNode.objects.filter(key="component:api-green").count(), 2)

    def test_rebuild_is_idempotent_and_skipped_when_nothing_changed(self):
        self.scenario()
        first = rebuild(self.app)
        edges = set(OpsEdge.objects.values_list("source__key", "target__key", "relation"))
        second = ensure_current(self.app)
        self.assertEqual(second.built_at, first.built_at)
        rebuild(self.app)
        self.assertEqual(
            set(OpsEdge.objects.values_list("source__key", "target__key", "relation")), edges
        )

    def test_a_retired_record_leaves_the_graph(self):
        self.scenario()
        rebuild(self.app)
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        change.active = False
        change.save()
        ensure_current(self.app)
        self.assertFalse(OpsNode.objects.filter(label__startswith="CHG1 ").exists())
        self.assertNotIn(("CHG1", "INC9"), self.keys("before"))

    def test_person_made_edges_survive_a_rebuild(self):
        self.scenario()
        rebuild(self.app)
        incident = OpsNode.objects.get(label__startswith="INC9 ")
        change = OpsNode.objects.get(label__startswith="CHG1 ")
        OpsEdge.objects.create(
            application=self.app,
            source=incident,
            target=change,
            relation="confirmed",
            created_by=self.owner,
        )
        rebuild(self.app)
        self.assertIn(("INC9", "CHG1"), self.keys("confirmed"))

    def test_a_hand_added_incident_is_in_the_graph_at_once(self):
        entry = create_manual_incident(
            self.owner,
            self.app,
            {
                "title": "Portal login fails",
                "description": "401 Unauthorized.",
                "number": "",
                "service": "Portal",
                "ci": "portal-web",
                "priority": "2",
            },
        )
        node = OpsNode.objects.get(key=f"incident:{entry.pk}")
        self.assertEqual(
            {edge.target.label for edge in node.outgoing.all()},
            {"portal-web", "Portal", "auth failure"},
        )

    def test_clear_removes_everything_including_confirmed_edges(self):
        self.scenario()
        rebuild(self.app)
        rebuild(self.other)
        clear([self.app.pk])
        self.assertFalse(OpsNode.objects.filter(application=self.app).exists())
        self.assertFalse(OperationsGraph.objects.filter(application=self.app).exists())
        self.assertTrue(OperationsGraph.objects.filter(application=self.other).exists())


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphUseTests(Fixtures, TestCase):
    """Phases 2 to 5: triage walks the graph, people see it, it learns, it reads documents."""

    def runbook(self, text, number=1):
        """A published knowledge-graph version holding one quoted passage."""
        from django.utils import timezone

        from platform_core.models import Document, GraphRevision, KnowledgeEntry

        entry = add_knowledge(self.owner, self.app.pk, "Deployment runbook", text)
        # An uploaded document, as a runbook is: a connector import has none.
        document = Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name=f"runbook-{number}.md",
            size=len(text),
            sha256="0" * 64,
            status="ready",
        )
        KnowledgeEntry.objects.filter(pk=entry.pk).update(document=document)
        quote = text.splitlines()[1]
        GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint="x",
            data={
                "nodes": [
                    {"id": "a", "label": "Deployment", "kind": "section"},
                    {"id": "b", "label": "Rollback", "kind": "section"},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "relation": "describes",
                        "knowledge_id": str(entry.pk),
                        "digest": entry.digest,
                        "evidence": quote,
                        "line": 2,
                    }
                ],
                "sources": [{"id": str(entry.pk)}],
            },
            published_at=timezone.now(),
        )
        return entry, quote

    # Phase 2 ------------------------------------------------------------

    def test_triage_pack_is_the_walk_and_every_item_has_its_path(self):
        from platform_core.serviceops_triage import evidence_pack, model_evidence

        current = self.scenario()
        pack, _ = evidence_pack(self.app, current)
        kinds = {row["kind"] for row in pack}
        self.assertEqual(kinds, {"precedent", "change", "related_open"})
        change = next(row for row in pack if row["kind"] == "change")
        self.assertEqual(change["path"], "INC9 → api-green → CHG1 (2.2 h before)")
        self.assertTrue(all(row["path"] for row in pack))
        shown = model_evidence(pack)
        # The path goes to the model in the title; the quotable excerpt is untouched.
        self.assertIn(
            "[found via: INC9 → api-green → CHG1",
            shown[[r["kind"] for r in pack].index("change")]["title"],
        )
        self.assertEqual([row["excerpt"] for row in shown], [row["excerpt"] for row in pack])

    def test_page_and_pack_show_the_same_evidence(self):
        from platform_core.serviceops_triage import evidence_pack

        current = self.scenario()
        response = self.client.get(
            reverse("serviceops", args=[self.app.pk]), {"incident": current.pk}
        )
        self.assertEqual(response.status_code, 200)
        page = {
            str(row["entry"].pk)
            for key in ("precedents", "changes", "related_open")
            for row in response.context[key]
        }
        pack, _ = evidence_pack(self.app, current)
        self.assertEqual(page, {row["id"] for row in pack})
        self.assertContains(response, 'class="ops-path"')

    def test_an_edited_record_drops_out_at_read_even_before_a_rebuild(self):
        from platform_core.models import KnowledgeEntry
        from platform_core.ops_graph import neighbourhood

        current = self.scenario()
        rebuild(self.app)
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        # Content changed underneath the digest: the fingerprint cannot see
        # this, so only verification at read can.
        KnowledgeEntry.objects.filter(pk=change.pk).update(content=change.content + " edited")
        self.assertEqual(neighbourhood(self.app, current)["changes"], [])

    def test_the_second_step_is_walking_the_graph(self):
        from platform_core.serviceops_triage import STEPS

        self.assertEqual(STEPS[1][:2], ("evidence", "Walk the graph"))

    # Phase 3 ------------------------------------------------------------

    def test_incident_page_draws_the_map_around_it(self):
        current = self.scenario()
        response = self.client.get(
            reverse("serviceops", args=[self.app.pk]), {"incident": current.pk}
        )
        self.assertContains(response, "Around this incident")
        self.assertContains(response, 'class="ops-map"')
        kinds = {item["kind"] for item in response.context["map"]["items"]}
        self.assertTrue({"incident", "component", "change", "precedent", "related"} <= kinds)
        # The change hangs off the component it was reached through, not the middle.
        component = next(
            i["id"] for i in response.context["map"]["items"] if i["kind"] == "component"
        )
        change = next(i["id"] for i in response.context["map"]["items"] if i["kind"] == "change")
        self.assertIn(
            (component, change),
            {(line["a"], line["b"]) for line in response.context["map"]["lines"]},
        )

    def test_operations_layer_in_the_knowledge_explorer(self):
        self.scenario()
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "operations"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="knowledge-graph-data"')
        kinds = {node["kind"] for node in response.context["data"]["nodes"]}
        self.assertTrue({"incident", "change", "component", "service", "symptom"} <= kinds)
        self.assertContains(response, "?tab=operations")

    def test_operations_layer_of_another_application_is_not_found(self):
        self.incident("INC77", "Export times out", "504.", app=self.other)
        ApplicationGrant.objects.filter(application=self.other, user=self.owner).delete()
        response = self.client.get(reverse("graph", args=[self.other.pk]), {"tab": "operations"})
        self.assertEqual(response.status_code, 404)

    # Phase 4 ------------------------------------------------------------

    def idea(self, incident, cited):
        """A finished run on `incident` with one idea citing `cited`."""
        from platform_core.models import TriageHypothesis, TriageRun

        run = TriageRun.objects.create(
            application=self.app,
            incident=incident,
            requested_by=self.owner,
            number=1,
            status="completed",
            incident_digest=incident.digest,
            pack_digest="x",
        )
        return TriageHypothesis.objects.create(
            run=run,
            rank=1,
            statement="The deploy broke it.",
            next_step="Compare versions.",
            citations=[{"id": str(cited.pk), "quote": "Rolled out a new release."}],
        )

    def verdict(self, hypothesis, **values):
        return self.client.post(
            reverse("serviceops-verdict", args=[self.app.pk, hypothesis.pk]), values
        )

    def test_a_confirmed_cause_is_the_first_evidence_for_the_next_similar_incident(self):
        from platform_core.serviceops_triage import evidence_pack

        current = self.scenario()
        rebuild(self.app)
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        # INC2 is the resolved precedent; its cause gets confirmed.
        precedent = OpsNode.objects.get(label__startswith="INC2 ").entry
        hypothesis = self.idea(precedent, change)
        self.assertEqual(
            self.verdict(hypothesis, verdict="accepted", cause=str(change.pk)).status_code, 302
        )
        edge = OpsEdge.objects.get(relation="confirmed")
        self.assertEqual((edge.source.entry_id, edge.target.entry_id), (precedent.pk, change.pk))
        rebuild(self.app)  # a person's edge survives a rebuild
        pack, _ = evidence_pack(self.app, current)
        self.assertEqual(pack[0]["kind"], "confirmed_cause")
        self.assertEqual(pack[0]["id"], str(change.pk))
        self.assertIn("confirmed by owner as the cause of INC2", pack[0]["reasons"][0])
        self.assertEqual(pack[0]["path"], "INC9 → similar INC2 → confirmed cause: CHG1")

    def test_a_cause_the_idea_did_not_cite_is_refused(self):
        self.scenario()
        rebuild(self.app)
        precedent = OpsNode.objects.get(label__startswith="INC2 ").entry
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        other_change = OpsNode.objects.get(label__startswith="CHG2 ").entry
        hypothesis = self.idea(precedent, change)
        self.assertEqual(
            self.verdict(hypothesis, verdict="accepted", cause=str(other_change.pk)).status_code,
            404,
        )
        self.assertFalse(OpsEdge.objects.filter(relation="confirmed").exists())

    def test_not_right_never_records_a_cause(self):
        self.scenario()
        rebuild(self.app)
        precedent = OpsNode.objects.get(label__startswith="INC2 ").entry
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        self.verdict(self.idea(precedent, change), verdict="rejected", cause=str(change.pk))
        self.assertFalse(OpsEdge.objects.filter(relation="confirmed").exists())

    def test_withdrawing_a_verdict_removes_what_the_graph_learned(self):
        from platform_core.models import TriageVerdict

        self.scenario()
        rebuild(self.app)
        precedent = OpsNode.objects.get(label__startswith="INC2 ").entry
        change = OpsNode.objects.get(label__startswith="CHG1 ").entry
        hypothesis = self.idea(precedent, change)
        self.verdict(hypothesis, verdict="partial", cause=str(change.pk))
        mine = TriageVerdict.objects.get()
        self.assertEqual(mine.actual_cause_id, change.pk)
        self.verdict(hypothesis, action="retract", verdict_id=str(mine.pk))
        self.assertFalse(TriageVerdict.objects.exists())
        self.assertFalse(OpsEdge.objects.filter(relation="confirmed").exists())

    def test_confirmed_history_counts_in_the_score(self):
        from platform_core.serviceops_triage import SCORE_LABELS, SCORE_WEIGHTS, _score

        self.assertAlmostEqual(sum(SCORE_WEIGHTS.values()), 1.0)
        self.assertEqual(set(SCORE_WEIGHTS), set(SCORE_LABELS))
        row = {
            "id": "c",
            "kind": "confirmed_cause",
            "reasons": ["confirmed"],
            "path": "",
            "as_of": "2026-09-20T10:00:00+00:00",
            "own": False,
        }
        idea = [{"citations": [{"id": "c", "quote": "x"}]}]
        _, components, _ = _score([row], idea)
        self.assertEqual(components["history"], 0.7)

    # Phase 5 ------------------------------------------------------------

    def test_a_runbook_that_names_the_component_is_reached_through_it(self):
        from platform_core.ops_graph import neighbourhood
        from platform_core.serviceops_triage import evidence_pack

        current = self.scenario()
        runbook, quote = self.runbook(
            "# Deploy\nRoll back by moving traffic to api-green and draining the standby.\nEnd."
        )
        walk = neighbourhood(self.app, current)
        passage = next(row for row in walk["passages"] if row["id"] == str(runbook.pk))
        self.assertEqual(passage["excerpt"], quote)
        self.assertEqual(passage["path"][:2], ["INC9", "api-green"])
        mentions = OpsEdge.objects.get(relation="mentions")
        self.assertEqual(mentions.target.label, "api-green")
        pack, available = evidence_pack(self.app, current)
        self.assertTrue(available)
        self.assertIn(
            str(runbook.pk), {row["id"] for row in pack if row["kind"] == "published_knowledge"}
        )

    def test_a_longer_name_does_not_match_a_shorter_component(self):
        self.scenario()
        self.runbook("# Deploy\nThe api-green-canary deployment is separate.\nEnd.")
        rebuild(self.app)
        self.assertFalse(OpsEdge.objects.filter(relation="mentions").exists())

    def test_an_unpublished_or_changed_runbook_is_never_used(self):
        from platform_core.models import GraphRevision, KnowledgeEntry
        from platform_core.ops_graph import neighbourhood

        current = self.scenario()
        runbook, _ = self.runbook("# Deploy\nRoll back by moving traffic to api-green now.\nEnd.")
        self.assertTrue(neighbourhood(self.app, current)["passages"])
        KnowledgeEntry.objects.filter(pk=runbook.pk).update(content="# Deploy\nRewritten.\nEnd.")
        self.assertEqual(neighbourhood(self.app, current)["passages"], [])
        KnowledgeEntry.objects.filter(pk=runbook.pk).update(content=runbook.content)
        GraphRevision.objects.update(published_at=None)
        self.assertEqual(neighbourhood(self.app, current)["passages"], [])

    def test_an_imported_ticket_is_never_a_document_passage(self):
        """Live CareOps: the published graph held the tickets too, and a ticket's
        own "CI: api-green" header line "mentioned" the component."""
        from django.utils import timezone

        from platform_core.models import GraphRevision

        current = self.scenario()
        rebuild(self.app)
        precedent = OpsNode.objects.get(label__startswith="INC2 ").entry
        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="x",
            data={
                "nodes": [
                    {"id": "a", "label": "INC2", "kind": "record"},
                    {"id": "b", "label": "api-green", "kind": "value"},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "relation": "CI",
                        "knowledge_id": str(precedent.pk),
                        "digest": precedent.digest,
                        "evidence": "CI: api-green",
                        "line": 7,
                    }
                ],
                "sources": [{"id": str(precedent.pk)}],
            },
            published_at=timezone.now(),
        )
        rebuild(self.app)
        self.assertFalse(OpsEdge.objects.filter(relation="mentions").exists())
        from platform_core.ops_graph import neighbourhood

        self.assertEqual(neighbourhood(self.app, current)["passages"], [])
