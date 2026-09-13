from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ChatMigrationTests(TransactionTestCase):
    """The whole chat history chain: flat turns -> conversations -> message pairs."""

    def test_existing_history_is_preserved_through_every_migration(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("platform_core", "0011_graphrevision")]
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            User = apps.get_model("platform_core", "User")
            Organization = apps.get_model("platform_core", "Organization")
            Portfolio = apps.get_model("platform_core", "Portfolio")
            Product = apps.get_model("platform_core", "Product")
            Application = apps.get_model("platform_core", "Application")
            Turn = apps.get_model("platform_core", "ChatTurn")
            users = [User.objects.create(username=f"legacy-{i}") for i in range(2)]
            org = Organization.objects.create(name="Legacy")
            portfolio = Portfolio.objects.create(name="P", organization=org)
            product = Product.objects.create(name="P", portfolio=portfolio)
            applications = [
                Application.objects.create(name=f"App-{i}", product=product) for i in range(2)
            ]
            ids = []
            for app in applications:
                for user in users:
                    turn = Turn.objects.create(
                        application=app,
                        user=user,
                        question="Original question",
                        answer="Original answer",
                        citations=[{"id": "s1", "title": "Source", "digest": "d1"}],
                    )
                    ids.append(turn.pk)

            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            apps = executor.loader.project_state(latest).apps
            Conversation = apps.get_model("platform_core", "ChatConversation")
            Message = apps.get_model("platform_core", "ChatMessage")

            # 0012 gave each (application, user) pair its own conversation; 0015 split
            # every turn into a question and an answer.
            self.assertEqual(Conversation.objects.count(), 4)
            self.assertEqual(Message.objects.count(), 8)
            self.assertEqual(Message.objects.filter(role="user").count(), 4)
            self.assertEqual(Message.objects.filter(role="assistant").count(), 4)

            # Assistant IDs are carried over so existing audit events still resolve.
            self.assertEqual(
                set(Message.objects.filter(role="assistant").values_list("pk", flat=True)),
                set(ids),
            )

            for conversation in Conversation.objects.all():
                messages = list(conversation.messages.order_by("sequence"))
                self.assertEqual([m.role for m in messages], ["user", "assistant"])
                self.assertEqual([m.sequence for m in messages], [0, 1])
                question, answer = messages
                self.assertEqual(question.body, "Original question")
                self.assertEqual(answer.body, "Original answer")
                self.assertEqual(answer.citations[0]["digest"], "d1")
                self.assertEqual(question.citations, [])
                self.assertEqual(answer.status, "complete")
                # Original timestamps survive auto_now_add on the new rows.
                self.assertEqual(question.created_at, conversation.created_at)
                self.assertEqual(answer.created_at, conversation.created_at)
                for message in messages:
                    self.assertEqual(message.user_id, conversation.user_id)
                    self.assertEqual(message.application_id, conversation.application_id)
        finally:
            MigrationExecutor(connection).migrate(latest)
