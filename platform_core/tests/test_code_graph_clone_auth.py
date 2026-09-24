"""How the Code Graph clone presents a GitHub token to git.

GitHub's REST API accepts a token as `Authorization: Bearer`; its git-over-HTTPS
endpoint does not. With a Bearer header git fell back to asking for a username,
prompts are off, and every private clone failed as "refused the credential"
while the same token read the repository through the API.
"""

import base64
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from platform_core.code_graph_clone import _environment


class CloneAuthTests(SimpleTestCase):
    def config(self, token):
        with tempfile.TemporaryDirectory() as name:
            environment = _environment(Path(name), token)
            return Path(environment["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")

    def test_the_token_is_sent_as_basic_auth_git_accepts(self):
        text = self.config("ghp_example")
        expected = base64.b64encode(b"x-access-token:ghp_example").decode()
        self.assertIn(f"Authorization: Basic {expected}", text)
        self.assertNotIn("Bearer", text)

    def test_no_token_sends_no_header(self):
        self.assertNotIn("Authorization", self.config(""))
