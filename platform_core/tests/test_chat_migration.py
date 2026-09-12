from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ChatMigrationTests(TransactionTestCase):
    def test_existing_history_preserved_per_user_and_application(self):
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
                    )
                    ids.append(turn.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            apps = executor.loader.project_state(latest).apps
            Turn = apps.get_model("platform_core", "ChatTurn")
            Conversation = apps.get_model("platform_core", "ChatConversation")
            self.assertEqual(Conversation.objects.count(), 4)
            self.assertEqual(Turn.objects.count(), 4)
            for turn in Turn.objects.filter(pk__in=ids).select_related("conversation"):
                self.assertEqual(turn.user_id, turn.conversation.user_id)
                self.assertEqual(turn.application_id, turn.conversation.application_id)
                self.assertEqual(turn.answer, "Original answer")
                self.assertEqual(turn.created_at, turn.conversation.created_at)
        finally:
            MigrationExecutor(connection).migrate(latest)
