"""When an agent's answer yields no usable file, the run says why.

The implementation agent is told to leave out a file it cannot change and say
why in `notes`. A live run did exactly that, twice, and the run showed only "no
usable file changes" - the model's reason was discarded, and so was every clue
about which of four checks each entry had failed.
"""

from django.test import SimpleTestCase

from platform_core.code_factory import UnusableAnswer, collect_changes

SHOWN = [
    {"path": "src/api.py", "text": "old", "sha": "a"},
    {"path": "src/errors.py", "text": "same", "sha": "b"},
]


class CollectChangesTests(SimpleTestCase):
    def refusal(self, payload):
        with self.assertRaises(UnusableAnswer) as raised:
            collect_changes(payload, SHOWN)
        return raised.exception

    def test_the_models_own_reason_is_kept(self):
        error = self.refusal(
            {"files": [], "notes": "services/consent.py was not shown, so I cannot call it."}
        )
        self.assertIn("It said: services/consent.py was not shown", " ".join(error.messages))
        self.assertIn("notes:", error.sample)

    def test_each_dropped_entry_says_why(self):
        error = self.refusal(
            {
                "files": [
                    {"path": "src/api.py", "content": "new"},  # no number
                    {"file": 2, "content": "same"},  # unchanged
                ]
            }
        )
        self.assertIn("is not one of the 2 shown (keys: content, path)", error.sample)
        self.assertIn("src/errors.py: returned unchanged", error.sample)

    def test_an_empty_answer_with_no_reason_says_so(self):
        error = self.refusal({"files": []})
        self.assertIn("gave no reason", " ".join(error.messages))

    def test_a_usable_change_is_still_taken(self):
        changes = collect_changes({"files": [{"file": 1, "content": "new"}]}, SHOWN)
        self.assertEqual(changes, [{"path": "src/api.py", "content": "new", "sha": "a"}])

    def test_entries_past_the_number_shown_are_still_read(self):
        """A live test author was shown two test files and returned four entries:
        the two source files it was not given first, then its two tests. Reading
        only the first two threw away the only entries that counted."""
        tests = [
            {"path": "tests/test_fhir.py", "text": "old fhir", "sha": "t1"},
            {"path": "tests/test_consent.py", "text": "old consent", "sha": "t2"},
        ]
        changes = collect_changes(
            {
                "files": [
                    {"file": "src/carepath/api/deps.py", "content": "not shown"},
                    {"file": "src/carepath/api/routes_fhir.py", "content": "not shown"},
                    {"file": 1, "content": "new fhir"},
                    {"file": 2, "content": "new consent"},
                ]
            },
            tests,
            agent="test author",
        )
        self.assertEqual(
            [change["path"] for change in changes], ["tests/test_fhir.py", "tests/test_consent.py"]
        )

    def test_a_file_may_be_named_by_the_exact_path_it_was_shown_with(self):
        changes = collect_changes({"files": [{"file": "src/api.py", "content": "new"}]}, SHOWN)
        self.assertEqual(changes, [{"path": "src/api.py", "content": "new", "sha": "a"}])

    def test_a_path_that_was_not_shown_is_never_taken(self):
        error = self.refusal({"files": [{"file": "src/secret.py", "content": "new"}]})
        self.assertIn("'src/secret.py' is not one of the 2 shown", error.sample)

    def test_one_change_per_file(self):
        changes = collect_changes(
            {
                "files": [
                    {"file": 1, "content": "first"},
                    {"file": "src/api.py", "content": "second"},
                ]
            },
            SHOWN,
        )
        self.assertEqual([change["content"] for change in changes], ["first"])

    def test_the_message_names_the_agent_that_answered(self):
        with self.assertRaises(UnusableAnswer) as raised:
            collect_changes({"files": []}, SHOWN, agent="test author")
        message = raised.exception.messages[0]
        self.assertIn("The test author returned no usable file changes", message)
