"""Code review: a change compared with the snapshot it starts from.

What these pin: the boundary (deny by default, a feature of its own, viewers
read and never start), a folder is re-read with git never run, a branch is
compared only on the files GitHub says it touches, the checks by rule, and the
AI half - opt-in, receipted, budgeted, every finding quoted and checked again
when the page is drawn.
"""

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import code_review
from platform_core.code_graph_ingest import index_repository, register_local
from platform_core.code_review import process_next_review, process_next_review_ai
from platform_core.graphs import build_graph
from platform_core.models import (
    AIBudget,
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    ApplicationGrant,
    CodeFile,
    CodeRepository,
    CodeReview,
    CodeSnapshot,
    DocumentAIResult,
    GraphRevision,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_code_factory import SETTINGS


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


EXPORT = (
    "from app import audit\n\n\n"
    "def export(site):\n"
    "    rows = Patient.objects.filter(site=site, consent_withdrawn=False)  # BR-05\n"
    "    audit.record(site)\n"
    "    return rows\n"
)
AUDIT = "def record(site):\n    return site\n"
VIEWS = "from app.export import export\n\n\ndef view(request):\n    return export(1)\n"
URLS = 'path("api/patients/<int:pk>/export", view)\n'


class ReviewBase(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        patcher = patch("platform_core.secrets.application_secret", return_value="key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0

    def configure(self, purpose="code_review"):
        return AIConfiguration.objects.update_or_create(
            application=self.app,
            purpose=purpose,
            defaults={
                "provider": "claude",
                "model": "claude-sonnet-5",
                "input_rate": Decimal("2"),
                "output_rate": Decimal("10"),
                "configured_by": self.owner,
                "enabled": True,
            },
        )[0]

    def model(self, *answers):
        """The provider adapter, so `invoke_ai` - grants, budget, receipt - runs for real."""
        replies = list(answers)

        def reply(config, question, citations, token, **options):
            self.calls += 1
            self.seen = (question, citations, options)
            return (
                {"id": f"req-{self.calls}",
                 "usage": {"prompt_tokens": 1200, "completion_tokens": 80}},
                replies.pop(0),
            )

        return patch("platform_core.claude_agents.completion", side_effect=reply)

    def publish(self, text):
        entry = add_knowledge(self.owner, self.app.pk, "Business requirements", text)
        data, quality = build_graph([entry], 1)
        GraphRevision.objects.create(
            application=self.app, number=1, data=data, quality=quality,
            published_at=timezone.now(), fingerprint="test",
        )
        return entry


@override_settings(**SETTINGS)
class LocalReviewTests(ReviewBase):
    def setUp(self):
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve() / "code"
        self.folder = self.root / "care"
        write(self.folder / "app/export.py", EXPORT)
        write(self.folder / "app/audit.py", AUDIT)
        write(self.folder / "app/views.py", VIEWS)
        write(self.folder / "app/urls.py", URLS)
        roots = override_settings(CODE_GRAPH_LOCAL_ROOTS=[str(self.root)])
        roots.enable()
        self.addCleanup(roots.disable)
        self.repository = register_local(self.owner, self.app.pk, str(self.folder))
        self.assertTrue(index_repository(self.repository))
        self.repository.refresh_from_db()
        self.snapshot = self.repository.snapshots.get()

    def change_folder(self):
        write(
            self.folder / "app/export.py",
            EXPORT.replace(", consent_withdrawn=False)  # BR-05", ")  # BR-05 dropped"),
        )
        write(self.folder / "app/serializers.py", "def serialise(row):\n    return row\n")
        (self.folder / "app/urls.py").unlink()

    def review(self, user=None):
        review = code_review.request_review(
            user or self.owner, self.app.pk, self.repository.pk, "local"
        )
        # git is never run in a folder: reading it must not start a process.
        with patch("subprocess.run", side_effect=AssertionError("git was run")):
            self.assertTrue(process_next_review())
        review.refresh_from_db()
        return review

    def test_working_changes_are_compared_with_the_snapshot(self):
        self.change_folder()
        review = self.review()
        self.assertEqual(review.status, "ready", review.error)
        changes = {row.path: row.change for row in review.files.all()}
        self.assertEqual(changes, {
            "app/export.py": "modified",
            "app/serializers.py": "added",
            "app/urls.py": "removed",
        })
        export = review.files.get(path="app/export.py")
        self.assertIn("-    rows = Patient.objects.filter(site=site, consent_withdrawn=False)",
                      export.diff)
        self.assertEqual(export.head_digest, hashlib.sha256(
            export.head_content.encode()).hexdigest())
        summary = review.summary
        self.assertEqual(summary["files"], 3)
        # views.py imports export.py, so a change to it reaches one file.
        facts = {item["path"]: item for item in summary["file_facts"]}
        self.assertEqual(facts["app/export.py"]["dependents"], 1)
        self.assertIn({"file": "app/urls.py", "method": "*",
                       "path": "/api/patients/*/export", "change": "removed"}, summary["routes"])
        texts = " ".join(check["text"] for check in summary["checks"])
        self.assertIn("Source changed with no test changed", texts)
        self.assertEqual([item["identifier"] for item in summary["named"]], ["BR-05"])

    def test_an_unchanged_folder_has_nothing_to_review(self):
        review = self.review()
        self.assertEqual(review.status, "ready")
        self.assertFalse(review.files.exists())

    def test_the_form_works_without_script_and_lands_on_the_review(self):
        self.change_folder()
        response = self.client.post(
            reverse("code-reviews", args=[self.app.pk]),
            {"repository": str(self.repository.pk), "kind": "local"},
        )
        review = CodeReview.objects.get()
        self.assertRedirects(response, reverse("code-review", args=[self.app.pk, review.pk]))
        page = self.client.get(reverse("code-review", args=[self.app.pk, review.pk]))
        self.assertContains(page, "data-document-pending")
        process_next_review()
        page = self.client.get(reverse("code-review", args=[self.app.pk, review.pk]))
        self.assertContains(page, "app/serializers.py")
        self.assertContains(page, "Checks by rule")
        self.assertNotContains(page, "data-document-pending")

    def test_access_is_denied_by_default(self):
        review = self.review()
        detail = reverse("code-review", args=[self.app.pk, review.pk])
        listing = reverse("code-reviews", args=[self.app.pk])
        # No grant: the application is not confirmed to exist.
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(listing).status_code, 404)
        self.assertEqual(self.client.get(detail).status_code, 404)
        # A review is found only inside its own application.
        self.client.force_login(self.owner)
        other = reverse("code-review", args=[self.other.pk, review.pk])
        self.assertEqual(self.client.get(other).status_code, 404)

    def test_a_feature_of_its_own(self):
        ApplicationFeature.objects.create(application=self.app, key="code_review", enabled=False)
        self.assertEqual(
            self.client.get(reverse("code-reviews", args=[self.app.pk])).status_code, 403
        )
        page = self.client.get(reverse("code-graph", args=[self.app.pk]))
        self.assertNotContains(page, reverse("code-reviews", args=[self.app.pk]))

    def test_viewers_read_reviews_and_never_start_them(self):
        review = self.review()
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get(reverse("code-review", args=[self.app.pk, review.pk])).status_code,
            200,
        )
        response = self.client.post(
            reverse("code-reviews", args=[self.app.pk]),
            {"repository": str(self.repository.pk), "kind": "local"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("code-review", args=[self.app.pk, review.pk]), {"action": "ai"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CodeReview.objects.count(), 1)

    def test_revoking_the_grant_stops_a_queued_review(self):
        review = code_review.request_review(self.owner, self.app.pk, self.repository.pk, "local")
        ApplicationGrant.objects.filter(user=self.owner).delete()
        process_next_review()
        review.refresh_from_db()
        self.assertEqual(review.status, "failed")

    def test_the_requirements_a_change_names_come_from_the_published_graph(self):
        self.publish(
            "| Requirement | Statement |\n| --- | --- |\n"
            "| BR-05 | Exports must exclude patients who withdrew consent |"
        )
        self.change_folder()
        review = self.review()
        page = self.client.get(reverse("code-review", args=[self.app.pk, review.pk]))
        self.assertContains(page, "Business requirements")
        self.assertContains(page, "A reference, not proof it is met")

    # ------------------------------------------------------------- AI review

    def ask(self, review, *answers):
        with self.model(*answers):
            response = self.client.post(
                reverse("code-review", args=[self.app.pk, review.pk]), {"action": "ai"}
            )
            self.assertEqual(response.status_code, 302)
            process_next_review_ai()
        review.refresh_from_db()
        return review

    def test_nothing_is_asked_until_someone_presses_the_button(self):
        self.configure()
        self.change_folder()
        with self.model():
            review = self.review()
        self.assertEqual(self.calls, 0)
        self.assertEqual(review.ai_status, "")
        self.assertFalse(AIUsage.objects.exists())

    def test_the_button_needs_the_purpose_configured(self):
        self.change_folder()
        review = self.review()
        page = self.client.get(reverse("code-review", args=[self.app.pk, review.pk]))
        self.assertContains(page, "Code review in AI settings")
        response = self.client.post(
            reverse("code-review", args=[self.app.pk, review.pk]), {"action": "ai"}, follow=True
        )
        self.assertContains(response, "set up Code review in AI settings")
        review.refresh_from_db()
        self.assertEqual(review.ai_status, "")

    def test_only_findings_whose_quotes_are_found_are_kept(self):
        self.configure()
        self.change_folder()
        review = self.review()
        answer = json.dumps({"findings": [
            {"severity": "high", "category": "correctness",
             "title": "Withdrawn-consent patients are exported again",
             "explanation": "The consent filter was removed.",
             "quotes": [{"source": "diff:app/export.py",
                         "text": "rows = Patient.objects.filter(site=site)"}]},
            {"severity": "low", "category": "design", "title": "Invented",
             "quotes": [{"source": "diff:app/export.py", "text": "this line is not there"}]},
            {"severity": "medium", "category": "requirement", "title": "Code only",
             "quotes": [{"source": "diff:app/export.py", "text": "audit.record(site)"}]},
        ]})
        review = self.ask(review, answer)
        self.assertEqual(review.ai_status, "ready", review.ai_error)
        result = review.ai_result
        self.assertEqual([f["title"] for f in result["findings"]],
                         ["Withdrawn-consent patients are exported again"])
        self.assertEqual(result["dropped"], 2)
        self.assertEqual(result["dropped_reasons"], {
            "quote not found": 1, "requirement without both quotes": 1,
        })
        usage = AIUsage.objects.get()
        self.assertEqual((usage.purpose, usage.input_tokens), ("code_review", 1200))
        # What the model read: the diff, and the file that depends on it.
        ids = [item["id"] for item in self.seen[1]]
        self.assertIn("diff:app/export.py", ids)
        self.assertTrue(any(item.startswith("code:") for item in ids))
        page = self.client.get(reverse("code-review", args=[self.app.pk, review.pk]))
        self.assertContains(page, "Withdrawn-consent patients are exported again")
        self.assertContains(page, "2 findings left out")

    def test_the_same_change_is_never_paid_for_twice(self):
        self.configure()
        self.change_folder()
        review = self.review()
        answer = json.dumps({"findings": []})
        self.ask(review, answer)
        self.ask(review)
        self.assertEqual(self.calls, 1)
        self.assertEqual(AIUsage.objects.count(), 1)
        self.assertEqual(DocumentAIResult.objects.filter(task="change").count(), 1)

    def test_the_budget_is_checked_before_the_call(self):
        self.configure()
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("0"))
        self.change_folder()
        review = self.ask(self.review())
        self.assertEqual(self.calls, 0)
        self.assertEqual(review.ai_status, "failed")
        self.assertIn("AI budget", review.ai_error)

    def test_a_requirement_finding_is_checked_against_the_live_source(self):
        entry = self.publish(
            "| Requirement | Statement |\n| --- | --- |\n"
            "| BR-05 | Exports must exclude patients who withdrew consent |"
        )
        self.configure()
        self.change_folder()
        review = self.review()
        answer = json.dumps({"findings": [
            {"severity": "high", "category": "requirement", "title": "Breaks BR-05",
             "explanation": "The filter BR-05 needs is gone.",
             "quotes": [
                 {"source": "diff:app/export.py",
                  "text": "rows = Patient.objects.filter(site=site)"},
                 {"source": str(entry.pk),
                  "text": "Exports must exclude patients who withdrew consent"},
             ]},
        ]})
        review = self.ask(review, answer)
        self.assertEqual(len(review.ai_result["findings"]), 1, review.ai_result)
        detail = reverse("code-review", args=[self.app.pk, review.pk])
        self.assertContains(self.client.get(detail), "Breaks BR-05")
        # Retired since: the finding is not shown as if it still held.
        entry.active = False
        entry.save(update_fields=["active"])
        page = self.client.get(detail)
        self.assertNotContains(page, "Breaks BR-05")
        self.assertContains(page, "1 no longer shown")


@override_settings(**SETTINGS)
class BranchReviewTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, name="acme/care", external_id="acme/care",
            status="ready", source_url="https://github.com/acme/care", provider="github",
        )
        self.snapshot = CodeSnapshot.objects.create(
            repository=self.repository, number=1, commit_sha="a" * 40, manifest_digest="m",
        )
        for path, content in (("app/export.py", EXPORT), ("app/audit.py", AUDIT)):
            CodeFile.objects.create(
                snapshot=self.snapshot, path=path, content=content, language="python",
                digest=hashlib.sha256(content.encode()).hexdigest(),
            )

    def run_review(self, kind, compare, head_files, pull=None, **values):
        calls = []

        def call(url, token, **options):
            calls.append(url)
            return pull if "/pulls/" in url else compare

        review = code_review.request_review(
            self.owner, self.app.pk, self.repository.pk, kind, **values
        )
        with patch("platform_core.github_write.call", side_effect=call), patch(
            "platform_core.code_graph_clone.clone_sources",
            return_value=("c" * 40, "x", head_files, [], True),
        ) as clone:
            process_next_review()
        review.refresh_from_db()
        return review, calls, clone

    def test_only_the_files_the_branch_touches_are_compared(self):
        changed = EXPORT.replace(", consent_withdrawn=False", "")
        # audit.py differs in the clone too - main moved on - but the branch did
        # not touch it, so it must not read as this change undoing main's work.
        head = [("app/export.py", changed), ("app/audit.py", "def record(site):\n    pass\n")]
        review, calls, clone = self.run_review(
            "branch",
            {"merge_base_commit": {"sha": "a" * 40},
             "files": [{"filename": "app/export.py"}, {"filename": "README.md"}]},
            head,
            ref="feature/consent",
        )
        self.assertEqual(review.status, "ready", review.error)
        self.assertEqual([row.path for row in review.files.all()], ["app/export.py"])
        self.assertEqual(review.head_commit, "c" * 40)
        self.assertIn(f"/repos/acme/care/compare/{'a' * 40}...feature/consent", calls[0])
        self.assertEqual(clone.call_args.args[:2], ("acme/care", "feature/consent"))
        self.assertTrue(any("not source code" in w for w in review.warnings))

    def test_a_merge_base_other_than_the_snapshot_is_said(self):
        review, _, _ = self.run_review(
            "branch",
            {"merge_base_commit": {"sha": "b" * 40}, "files": [{"filename": "app/export.py"}]},
            [("app/export.py", EXPORT + "\n")],
            ref="old-branch",
        )
        self.assertTrue(any("not from the snapshot's commit" in w for w in review.warnings))

    def test_a_pull_request_from_a_fork_reads_the_fork(self):
        review, calls, clone = self.run_review(
            "pull_request",
            {"merge_base_commit": {"sha": "a" * 40}, "files": [{"filename": "app/export.py"}]},
            [("app/export.py", EXPORT + "\n")],
            pull={"head": {"ref": "fix", "repo": {"full_name": "someone/care"}}},
            number=7,
        )
        self.assertEqual(review.status, "ready", review.error)
        self.assertTrue(calls[0].endswith("/repos/acme/care/pulls/7"))
        self.assertIn("...someone:fix", calls[1])
        self.assertEqual(clone.call_args.args[:2], ("someone/care", "fix"))
        self.assertEqual((review.head_repository, review.ref), ("someone/care", "fix"))

    def test_a_branch_name_that_could_be_an_option_is_refused(self):
        from django.core.exceptions import ValidationError

        for ref in ("--upload-pack=x", "../main", ""):
            with self.subTest(ref=ref), self.assertRaises(ValidationError):
                code_review.request_review(
                    self.owner, self.app.pk, self.repository.pk, "branch", ref=ref
                )

    def test_working_changes_need_a_folder(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            code_review.request_review(self.owner, self.app.pk, self.repository.pk, "local")


class TestPathTests(TestCase):
    def test_test_files_by_convention(self):
        for path in ("tests/test_x.py", "app/test_x.py", "src/x.test.ts", "a/__tests__/b.js",
                     "pkg/x_test.go", "src/test/java/XTest.java"):
            with self.subTest(path=path):
                self.assertTrue(code_review.is_test(path))
        for path in ("app/export.py", "src/contest.py", "latest/x.js"):
            with self.subTest(path=path):
                self.assertFalse(code_review.is_test(path))
