"""Report whether a platform administrator exists.

Used by start-all to decide whether to offer first-run setup, and by -Install to
tell "already set up" apart from a real failure. Prints one word so the calling
shell needs no parsing, and reads nothing but the flag - no identity, no
password, nothing that would be worth leaking into a console log.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

import django  # noqa: E402

django.setup()

from platform_core.models import User  # noqa: E402

print("present" if User.objects.filter(is_platform_admin=True).exists() else "missing")
