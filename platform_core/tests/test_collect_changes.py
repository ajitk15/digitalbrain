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
