"""The Linux demo workspace survives deployment without keeping old dependencies."""

import os
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless

from scripts.init_vps_demo import initialize


@skipUnless(os.name == "posix", "VPS initialization uses Linux flock and symlinks.")
class VPSWorkspaceTests(TestCase):
    def test_redeployment_preserves_source_and_uses_image_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed, workspace, interpreter = root / "seed", root / "workspace", root / "venv"
            seed.mkdir()
            (seed / "app.py").write_text("original")
            initialize(seed, workspace, interpreter)
            source = workspace / "carepath" / "app.py"
            self.assertEqual(source.read_text(), "original")
            source.write_text("approved change")
            (seed / "app.py").write_text("new image seed")
            initialize(seed, workspace, interpreter)
            self.assertEqual(source.read_text(), "approved change")
            self.assertEqual((workspace / "carepath" / ".venv").readlink(), interpreter)

    def test_unexpected_interpreter_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed, workspace = root / "seed", root / "workspace"
            seed.mkdir()
            initialize(seed, workspace, root / "venv")
            with self.assertRaisesRegex(RuntimeError, "unexpected test environment"):
                initialize(seed, workspace, root / "different-venv")
