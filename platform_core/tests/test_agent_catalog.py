"""Where agents run, said where they run (`agent_catalog`, the agent badge and its page).

Pins that the catalog describes what the harness really does - each agent's tools
are the registry's list for its purpose, its purpose and feature exist - that its
state follows the feature, the AI settings task and the deployment's switch, that
its activity is counts from its own steps in this application only, and that the
popup page is held to application access like every screen.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from platform_core import agent_catalog, gateway
from platform_core.agent_runtime.registry import for_scope
from platform_core.agent_runtime.tools import ToolScope
from platform_core.models import (
    AI_PURPOSES,
    AgentStep,
    AIBudget,
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    ChatConversation,
    ChatMessage,
    User,
)
from platform_core.scorecard import code_snapshot
from platform_core.services import FEATURES

from . import test_documents

SWITCHES = dict.fromkeys(
    ("CODE_FACTORY_AGENT_TOOLS", "SERVICEOPS_TRIAGE_AGENT_TOOLS", "CODE_REVIEW_AGENT_TOOLS"), True
)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class CatalogTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        for purpose in ("chat", "knowledge_upkeep", "code_review", "plan_drafting",
                        "serviceops_triage"):
            AIConfiguration.objects.create(
                application=self.app, purpose=purpose, provider="claude",
                model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
                configured_by=self.owner,
            )

        # Tests mount no credential; one test below takes it away on purpose.
        reachable = patch("platform_core.ai.credential_problem", return_value=None)
        reachable.start()
        self.addCleanup(reachable.stop)
        # One indexed repository, so a Code Factory run here would be pinned to it.
        self.snapshot = code_snapshot(SimpleNamespace(app=self.app, people={"owner": self.owner}))

    def agent(self, key):
        return agent_catalog.get(key)

    # The registry -------------------------------------------------------

    def test_every_agent_names_what_exists_and_is_given_the_registrys_tools(self):
        agents = agent_catalog.agents()
        self.assertEqual(len({agent.key for agent in agents}), len(agents))
        step_fields = {field.name for field in AgentStep._meta.fields}
        for agent in agents:
            with self.subTest(agent=agent.key):
                self.assertIn(agent.purpose, gateway.PURPOSES)
                self.assertIn(agent.purpose, dict(AI_PURPOSES))
                self.assertIn(agent.feature, FEATURES)
                self.assertIn(agent.steps_field, step_fields)
                if agent.switch:
                    self.assertTrue(hasattr(settings, agent.switch))
                    self.assertTrue(agent.one_call)
                tools = agent_catalog.tools(agent)
                self.assertTrue(tools)
                self.assertEqual(
                    [t.name for t in tools],
                    [t.name for t in for_scope(ToolScope("u", "a", agent.purpose))],
                )

    # State ----------------------------------------------------------------

    def test_on_where_its_feature_task_and_switch_all_say_so(self):
        with override_settings(**SWITCHES):
            for agent in agent_catalog.agents():
                with self.subTest(agent=agent.key):
                    self.assertEqual(agent_catalog.status(agent, self.app)[0], "on")

    def test_a_switch_that_is_off_asks_once(self):
        with override_settings(CODE_REVIEW_AGENT_TOOLS=False):
            state, label, detail = agent_catalog.status(self.agent("code-review"), self.app)
        self.assertEqual((state, label), ("one-call", "One call"))
        self.assertIn("code_review_agent_tools = true", detail)
        # Chat has no switch: it is always an agent.
        self.assertEqual(agent_catalog.status(self.agent("chat"), self.app)[0], "on")

    def test_a_task_off_in_ai_settings_or_a_feature_off_is_off(self):
        AIConfiguration.objects.filter(application=self.app, purpose="chat").update(enabled=False)
        state, _, detail = agent_catalog.status(self.agent("chat"), self.app)
        self.assertEqual(state, "off")
        self.assertIn("AI settings", detail)
        ApplicationFeature.objects.create(application=self.app, key="knowledge", enabled=False)
        state, _, detail = agent_catalog.status(self.agent("draft-a-fix"), self.app)
        self.assertEqual(state, "off")
        self.assertIn("switched off", detail)

    def test_a_missing_credential_is_cant_run_and_says_why(self):
        with patch("platform_core.ai.credential_problem", return_value="Add a Claude key."):
            state, label, detail = agent_catalog.status(self.agent("chat"), self.app)
        self.assertEqual((state, label, detail), ("blocked", "Can't run", "Add a Claude key."))

    def spend(self, amount):
        AIUsage.objects.create(
            application=self.app, actor=self.owner, purpose="chat", provider="claude",
            model="claude-sonnet-5", request_id="r-1", input_tokens=1, output_tokens=1,
            amount=amount,
        )

    def test_a_spent_budget_is_cant_run(self):
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("5"))
        self.spend(Decimal("5"))
        state, _, detail = agent_catalog.status(self.agent("chat"), self.app)
        self.assertEqual(state, "blocked")
        self.assertIn("budget is used up", detail)

    def test_a_budget_with_room_for_one_turn_is_one_call(self):
        from platform_core import gateway
        from platform_core.agent_runtime.tools import RESULT_CHARACTERS

        agent = self.agent("chat")
        config = AIConfiguration.objects.get(application=self.app, purpose="chat")
        costs = gateway.turn_costs(
            config, agent.prompt_estimate, 1024, turns=agent.turns + 1,
            tool_characters=agent.tool_calls * RESULT_CHARACTERS,
        )
        # Room for the first turn and not the second: the harness gives no tools.
        AIBudget.objects.create(application=self.app, monthly_limit=(costs[0] + costs[1]) / 2)
        state, label, detail = agent_catalog.status(agent, self.app)
        self.assertEqual((state, label), ("one-call", "One call"))
        self.assertIn("about one turn", detail)
        AIBudget.objects.update(monthly_limit=costs[-1] * 2)
        self.assertEqual(agent_catalog.status(agent, self.app)[0], "on")

    def test_code_factory_with_no_code_to_read_asks_once(self):
        factory = self.agent("code-factory")
        with override_settings(CODE_FACTORY_AGENT_TOOLS=True):
            self.assertEqual(agent_catalog.status(factory, self.app)[0], "on")
            self.snapshot.repository.__class__.objects.filter(
                pk=self.snapshot.repository_id
            ).update(retired_at=self.snapshot.created_at)
            state, _, detail = agent_catalog.status(factory, self.app)
        self.assertEqual(state, "one-call")
        self.assertIn("No repository is indexed", detail)

    def test_the_turn_rule_is_admissions_own(self):
        from decimal import Decimal as D

        from platform_core import gateway

        self.assertEqual(gateway.affordable([D(1), D(3), D(6)], D(3)), 2)
        self.assertIsNone(gateway.headroom(self.app.pk))
        AIBudget.objects.create(application=self.app, monthly_limit=D("10"))
        self.spend(D("4"))
        self.assertEqual(gateway.headroom(self.app.pk), D("6"))

    # Activity ----------------------------------------------------------------

    def message(self, app):
        conversation = ChatConversation.objects.create(application=app, user=self.owner, title="t")
        return ChatMessage.objects.create(
            application=app, user=self.owner, conversation=conversation, role="assistant",
            body="", sequence=2,
        )

    def test_activity_is_counts_of_this_agents_steps_here(self):
        mine, theirs = self.message(self.app), self.message(self.other)
        for message, tool, outcome in (
            (mine, "search_knowledge", "found"), (mine, "fetch_source", "nothing"),
            (theirs, "search_knowledge", "found"),
        ):
            AgentStep.objects.create(
                application_id=message.application_id, message=message, purpose="chat",
                sequence=1, tool=tool, outcome=outcome,
            )
        found = agent_catalog.activity(self.agent("chat"), self.app)
        self.assertEqual((found["runs"], found["steps"]), (1, 2))
        self.assertEqual(dict(found["tools"]), {"search_knowledge": 1, "fetch_source": 1})
        self.assertEqual(dict(found["outcomes"]), {"found": 1, "nothing": 1})
        self.assertEqual(agent_catalog.activity(self.agent("draft-a-fix"), self.app)["steps"], 0)

    # Pages and badges --------------------------------------------------------

    def test_the_page_says_what_it_does_reads_and_keeps_safe(self):
        page = self.client.get(reverse("agent", args=[self.app.pk, "code-review"]))
        self.assertContains(page, "Code review agent")
        for tool in agent_catalog.tools(self.agent("code-review")):
            self.assertContains(page, f"<code>{tool.name}</code>", html=False)
        self.assertContains(page, "Nothing is posted to GitHub.")
        self.assertContains(page, "never the text it asked for or read")
        self.assertContains(page, reverse("agents", args=[self.app.pk]))

    def test_every_member_may_look_and_nobody_else_learns_it_exists(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("agents", args=[self.app.pk])).status_code, 200)
        stranger = User.objects.create_user("stranger")
        self.client.force_login(stranger, backend="django.contrib.auth.backends.ModelBackend")
        for url in (reverse("agents", args=[self.app.pk]),
                    reverse("agent", args=[self.app.pk, "chat"])):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_an_unknown_agent_is_not_found(self):
        url = reverse("agent", args=[self.app.pk, "nobody"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_index_lists_every_agent(self):
        page = self.client.get(reverse("agents", args=[self.app.pk]))
        for agent in agent_catalog.agents():
            self.assertContains(page, reverse("agent", args=[self.app.pk, agent.key]))

    def test_the_badge_sits_where_the_agent_starts_and_opens_as_a_popup(self):
        screens = (("chat", "chat"), ("learned", "draft-a-fix"), ("plans", "code-factory"))
        for screen, key in screens:
            with self.subTest(screen=screen):
                page = self.client.get(reverse(screen, args=[self.app.pk])).content.decode()
                link = reverse("agent", args=[self.app.pk, key])
                self.assertIn('class="agent-badge agent-badge-', page)
                self.assertRegex(page, rf'href="{link}" data-modal')

    # The Agent tag on what an agent made ------------------------------------

    def test_the_tag_shows_only_when_an_agent_made_tool_calls(self):
        from django.template import Context, Template

        page = Template('{% load agent_badges %}{% agent_tag application "chat" calls %}')
        for calls in (0, None, "", True):
            self.assertEqual(page.render(Context({"application": self.app, "calls": calls})), "")
        shown = page.render(Context({"application": self.app, "calls": 3}))
        self.assertIn('class="agent-tag"', shown)
        self.assertIn("3 steps", shown)
        self.assertIn(f'href="{reverse("agent", args=[self.app.pk, "chat"])}" data-modal', shown)

    def test_a_chat_answer_an_agent_read_for_is_tagged_and_one_that_did_not_is_not(self):
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="t"
        )
        for sequence, body in ((1, "Read for this"), (3, "Answered from what it was handed")):
            ChatMessage.objects.create(
                application=self.app, user=self.owner, conversation=conversation, role="user",
                body="q", sequence=sequence,
            )
            ChatMessage.objects.create(
                application=self.app, user=self.owner, conversation=conversation,
                role="assistant", body=body, status="complete", mode="ai", sequence=sequence + 1,
            )
        read = ChatMessage.objects.get(body="Read for this")
        for sequence, tool in enumerate(("search_knowledge", "fetch_source"), 1):
            AgentStep.objects.create(
                application=self.app, message=read, purpose="chat", sequence=sequence, tool=tool,
                outcome="found",
            )
        page = self.client.get(
            reverse("chat", args=[self.app.pk]), {"conversation": conversation.pk}
        ).content.decode()
        # One on the answer (a link), one on the conversation in the list (text).
        self.assertEqual(page.count('<a class="agent-tag"'), 1)
        self.assertEqual(page.count('<span class="agent-tag"'), 1)
        tagged = page[page.index(f'id="message-{read.pk}"'):]
        self.assertIn("2 steps", tagged[: tagged.index("</article>")])

    def test_a_triage_run_and_a_factory_step_are_counted_from_their_own_steps(self):
        import uuid

        from platform_core import factory_views

        # Step links carry no database constraint, so ids stand in for the rows.
        run, other, triage = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        rows = (
            {"run_id": run, "phase": "implementation", "purpose": "plan_drafting"},
            {"run_id": run, "phase": "implementation", "purpose": "plan_drafting"},
            {"run_id": run, "phase": "tests", "purpose": "plan_drafting"},
            {"run_id": other, "phase": "implementation", "purpose": "plan_drafting"},
            {"triage_id": triage, "purpose": "serviceops_triage"},
        )
        for sequence, fields in enumerate(rows, 1):
            AgentStep.objects.create(
                application=self.app, sequence=sequence, tool="read_file", outcome="found",
                **fields,
            )
        self.assertEqual(
            factory_views.agent_calls_by_phase(SimpleNamespace(pk=run)),
            {"implementation": 2, "tests": 1},
        )
        self.assertEqual(agent_catalog.calls("triage", [triage]), {triage: 1})
        self.assertEqual(agent_catalog.calls("triage", []), {})

    # The Agent tag on every list that shows what an agent made ------------------

    def test_mark_counts_each_rows_steps_in_one_query(self):
        first, second = self.message(self.app), self.message(self.app)
        AgentStep.objects.create(
            application=self.app, message=first, purpose="chat", sequence=1,
            tool="search_knowledge", outcome="found",
        )
        with self.assertNumQueries(1):
            rows = agent_catalog.mark([first, second, None], "message")
        self.assertEqual([row.agent_calls for row in rows], [1, 0])

    def test_a_conversation_an_agent_read_for_is_tagged_in_the_list_without_a_nested_link(self):
        message = self.message(self.app)
        AgentStep.objects.create(
            application=self.app, message=message, purpose="chat", sequence=1,
            tool="search_knowledge", outcome="found",
        )
        self.message(self.app)  # another conversation, nothing read
        page = self.client.get(reverse("chat", args=[self.app.pk])).content.decode()
        sidebar = page[page.index('class="conversation-link"'):page.index("conversation-header")]
        self.assertEqual(sidebar.count('<span class="agent-tag"'), 1)
        self.assertNotIn('<a class="agent-tag"', sidebar)

    def test_a_review_an_agent_read_further_for_is_tagged_in_the_review_list(self):
        from platform_core import review_pack

        change = review_pack.load_pack()["changes"]["rename-consent-current"]
        review = review_pack.build(self.owner, self.app, self.snapshot, change)
        review.ai_status = "ready"
        review.ai_result = {"findings": [], "tool_calls": 4}
        review.save(update_fields=["ai_status", "ai_result"])
        page = self.client.get(
            reverse("code-reviews", args=[self.app.pk]),
            {"repository": self.snapshot.repository_id},
        ).content.decode()
        self.assertIn("4 steps", page)
        self.assertIn(reverse("agent", args=[self.app.pk, "code-review"]), page)

    def test_ai_settings_shows_the_badge_on_every_task_an_agent_runs(self):
        page = self.client.get(reverse("ai-settings", args=[self.app.pk])).content.decode()
        for key in ("chat", "draft-a-fix", "code-review", "code-factory", "triage"):
            with self.subTest(agent=key):
                self.assertIn(reverse("agent", args=[self.app.pk, key]), page)
        row = page[page.index('id="task-conversation_title"'):]
        self.assertNotIn("agent-badge", row[: row.index("</div>")])

    # Where every agent is -----------------------------------------------------

    def test_settings_leads_every_member_to_the_agents_and_where_each_shows(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        index = reverse("agents", args=[self.app.pk])
        # Settings → AI → Agents, from any settings screen a viewer can open.
        self.assertContains(self.client.get(reverse("api-tokens", args=[self.app.pk])), index)
        page = self.client.get(index)
        self.assertContains(page, 'class="settings-sidebar"')
        for agent in agent_catalog.agents():
            with self.subTest(agent=agent.key):
                for line in agent.shown:
                    self.assertContains(page, escape(line))
        # A viewer is led to every screen it may open, and not to Learned.
        self.assertContains(page, reverse("chat", args=[self.app.pk]))
        self.assertNotContains(page, reverse("learned", args=[self.app.pk]))
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.assertContains(self.client.get(index), reverse("learned", args=[self.app.pk]))

    def test_every_agent_is_drawn_on_its_screens_and_names_one_that_exists(self):
        import re
        from pathlib import Path

        templates = Path(__file__).resolve().parents[2] / "templates"
        drawn = {}
        for path in templates.glob("*.html"):
            pattern = r'agent_(badge|tag) application "([a-z-]+)"'
            for kind, key in re.findall(pattern, path.read_text(encoding="utf-8")):
                drawn.setdefault(key, set()).add(kind)
        for agent in agent_catalog.agents():
            with self.subTest(agent=agent.key):
                self.assertTrue(agent.shown)
                self.assertTrue(agent.screen_label)
                reverse(agent.screen, args=[self.app.pk])
                # Its badge where it starts, and its tag on what it made.
                self.assertEqual(drawn.get(agent.key), {"badge", "tag"})
        self.assertEqual(set(drawn) - {agent.key for agent in agent_catalog.agents()}, set())

    def test_a_feature_screen_switch_shows_the_badge_of_each_agent_it_runs(self):
        page = self.client.get(reverse("application-features", args=[self.app.pk])).content
        page = page.decode()
        for agent in agent_catalog.agents():
            with self.subTest(agent=agent.key):
                row = page[page.index(f'name="feature_{agent.feature}"'):]
                row = row[: row.index('class="list-row"') if 'class="list-row"' in row else None]
                self.assertIn(reverse("agent", args=[self.app.pk, agent.key]), row)

    def test_api_access_says_the_chat_endpoint_answers_through_the_chat_agent(self):
        page = self.client.get(reverse("api-tokens", args=[self.app.pk]))
        self.assertContains(page, reverse("agent", args=[self.app.pk, "chat"]))
        ApplicationFeature.objects.update_or_create(
            application=self.app, key="chat_api", defaults={"enabled": False}
        )
        page = self.client.get(reverse("api-tokens", args=[self.app.pk]))
        self.assertNotContains(page, reverse("agent", args=[self.app.pk, "chat"]))
