"""Feature answers are memoised per request, and only per request.

feature_enabled runs two queries and is consulted repeatedly while a page
renders - navigation, view and several template tags all ask it - which put 16
of a documents page's 28 queries into feature lookups alone. The memo must not
weaken the rule that a switch is re-checked on every request.
"""

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from platform_core.models import Application, ApplicationFeature, FeatureSwitch
from platform_core.services import (
    begin_feature_cache,
    end_feature_cache,
    feature_enabled,
)

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class FeatureCacheTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_repeated_questions_inside_one_request_cost_two_queries(self):
        token = begin_feature_cache()
        try:
            with CaptureQueriesContext(connection) as captured:
                for _ in range(3):
                    for key in ("chat", "knowledge", "code_factory", "connectors"):
                        feature_enabled(key, self.app)
            self.assertLessEqual(len(captured), 2)
        finally:
            end_feature_cache(token)

    def test_without_a_request_scope_every_call_reads_the_database(self):
        """The worker and shell must never serve a stale switch."""
        with CaptureQueriesContext(connection) as captured:
            feature_enabled("chat", self.app)
            feature_enabled("chat", self.app)
        self.assertGreaterEqual(len(captured), 4)

    def test_a_switch_flipped_between_requests_takes_effect_immediately(self):
        url = reverse("chat", args=[self.app.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        FeatureSwitch.objects.create(key="chat", enabled=False)
        # A disabled feature is refused, not hidden: the application still exists.
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_an_application_switch_is_not_leaked_to_another_application(self):
        """One memo holds several applications; they must not blur together."""
        ApplicationFeature.objects.create(application=self.app, key="chat", enabled=False)
        token = begin_feature_cache()
        try:
            self.assertFalse(feature_enabled("chat", self.app))
            self.assertTrue(feature_enabled("chat", self.other))
        finally:
            end_feature_cache(token)

    def test_the_memo_agrees_with_an_uncached_read(self):
        for key in ("chat", "knowledge", "usage_reports", "document_uploads"):
            for app in (self.app, self.other, None):
                with self.subTest(key=key, app=app):
                    direct = feature_enabled(key, app)
                    token = begin_feature_cache()
                    try:
                        self.assertEqual(feature_enabled(key, app), direct)
                    finally:
                        end_feature_cache(token)

    def test_a_page_render_stays_well_under_its_old_query_count(self):
        """Measured on an application that has finished onboarding.

        While onboarding is unfinished the setup strip answers "what is still
        missing?" on every application screen, which costs seven queries. That
        is a bounded period and it is the whole point of the strip, so it is
        allowed to cost something - but it must not become a permanent tax,
        which is what `setup_completed_at` prevents and what the test below
        pins.
        """
        from django.utils import timezone

        Application.objects.filter(pk=self.app.pk).update(setup_completed_at=timezone.now())
        url = reverse("documents", args=[self.app.pk])
        self.client.get(url)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        # Twenty-eight before the feature cache; the panel listing where
        # documents came from costs one more, and the test below is what keeps
        # that one from becoming one per source.
        self.assertLess(len(captured), 21, "documents used to take 28 queries")

    def test_a_finished_application_pays_nothing_for_the_setup_strip(self):
        """The milestone is what makes the strip free once it is earned."""
        from django.utils import timezone

        from platform_core.templatetags.workspace import application_nav

        request = self.client.request().wsgi_request
        request.user = self.owner
        context = {"application": self.app, "request": request}

        with CaptureQueriesContext(connection) as unfinished:
            self.assertIsNotNone(application_nav(context)["setup"])

        Application.objects.filter(pk=self.app.pk).update(setup_completed_at=timezone.now())
        self.app.refresh_from_db()
        with CaptureQueriesContext(connection) as finished:
            self.assertIsNone(application_nav(context)["setup"])

        self.assertEqual(len(finished), 0)
        self.assertGreater(len(unfinished), 5)

    def test_the_source_panel_costs_one_query_however_many_sources_there_are(self):
        """The counts are annotated, not counted per row.

        A budget that is raised whenever something is added stops being a
        budget. What makes the extra query acceptable is that it is a fixed
        cost, so that is the part worth pinning.
        """
        from platform_core.knowledge_sources import register

        url = reverse("documents", args=[self.app.pk])
        self.client.get(url)
        with CaptureQueriesContext(connection) as one_source:
            register(self.owner, self.app, "github", "https://github.com/a/b/tree/main/x")
            self.client.get(url)
        for index in range(5):
            register(self.owner, self.app, "github", f"https://github.com/a/b/tree/main/{index}")
        with CaptureQueriesContext(connection) as six_sources:
            self.client.get(url)
        self.assertLessEqual(len(six_sources), len(one_source))
