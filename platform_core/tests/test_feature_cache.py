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

from platform_core.models import ApplicationFeature, FeatureSwitch
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
        url = reverse("documents", args=[self.app.pk])
        self.client.get(url)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        self.assertLess(len(captured), 20, "documents used to take 28 queries")
