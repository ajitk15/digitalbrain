"""Migration 0057: ServiceOps AI on for applications older than the purpose."""

from importlib import import_module

from django.apps import apps
from django.test import TestCase

from platform_core.models import AIConfiguration, Application, Organization, Portfolio, Product

enable = import_module("platform_core.migrations.0057_enable_serviceops_ai").enable


class EnableServiceOpsAITests(TestCase):
    def setUp(self):
        portfolio = Portfolio.objects.create(
            organization=Organization.objects.create(name="ACME"), name="P"
        )
        self.product = Product.objects.create(portfolio=portfolio, name="P")

    def app(self, name, **configs):
        app = Application.objects.create(product=self.product, name=name)
        for purpose, values in configs.items():
            AIConfiguration.objects.create(
                application=app,
                purpose=purpose,
                input_rate=3,
                output_rate=15,
                **{"provider": "claude", "model": "claude-sonnet-5", **values},
            )
        return app

    def serviceops(self, app):
        return AIConfiguration.objects.filter(application=app, purpose="serviceops_triage").first()

    def test_an_application_without_it_gets_it_on_copied_from_chat(self):
        app = self.app("CarePath", chat={"enabled": False})
        enable(apps, None)
        row = self.serviceops(app)
        self.assertTrue(row.enabled)
        copied = (row.provider, row.model, row.input_rate)
        self.assertEqual(copied, ("claude", "claude-sonnet-5", 3))

    def test_an_owners_existing_choice_is_left_alone(self):
        app = self.app("CareOps", chat={}, serviceops_triage={"enabled": False, "model": "x"})
        enable(apps, None)
        row = self.serviceops(app)
        self.assertEqual((row.enabled, row.model), (False, "x"))
        self.assertEqual(AIConfiguration.objects.filter(application=app).count(), 2)

    def test_no_model_is_invented_for_an_application_with_no_chat_setting(self):
        app = self.app("Bare")
        enable(apps, None)
        self.assertIsNone(self.serviceops(app))
