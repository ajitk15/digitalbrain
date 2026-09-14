"""Choose an AI provider and mount its credential, as part of first-run setup.

Why this exists at all - the question people ask when they have used the Claude
Agent SDK elsewhere and never needed a key:

The SDK runs the bundled Claude Code CLI, and that CLI resolves credentials in a
fixed order - ANTHROPIC_API_KEY, then CLAUDE_CODE_OAUTH_TOKEN, then an interactive
login stored in CLAUDE_CONFIG_DIR. Most SDK apps get the third for free from the
developer's own `claude` login. This platform deliberately blocks it: every run is
given a blanked environment with CLAUDE_CONFIG_DIR, HOME and USERPROFILE pointed
at an empty temporary directory, so one application can never spend another's
budget or the operator's, and AIUsage attribution means something. See
agent_runtime/credentials.py and the invariants in CLAUDE.md.

So a credential has to come from somewhere, and this script is where it is asked
for - once, at setup, instead of leaving someone to hand-create a file named after
a UUID. In development there is also the host-login route, which restores exactly
the behaviour those other SDK apps have.

The value is read hidden, never echoed, never logged, never placed in .env or the
database, and lands in the mounted secret directory like every other secret.
"""

import os
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from platform_core.agent_runtime.credentials import describe  # noqa: E402
from platform_core.models import Application  # noqa: E402

CONFIG = ROOT / "config/local.toml"
#: One shared file per provider, written here and copied to a per-application file
#: when an application is created. Nothing reads these at request time: the runtime
#: still resolves `<provider>_<application id>` and nothing else, so the
#: one-credential-per-application boundary is unchanged.
DEFAULT_NAMES = {"claude": "claude_default", "openai": "openai_default"}


def secret_path(name):
    return Path(settings.SECRET_DIRECTORY) / name


def write_secret(name, value):
    """Create or replace a mounted secret, owner-only from the moment it exists."""
    path = secret_path(name)
    if path.exists():
        path.unlink()
    # O_EXCL with the mode set at creation: the value is never briefly world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    # On Windows the mode argument is ignored; the directory's restricted ACL,
    # set by init_local.py, is what protects the file.
    return path


def set_host_login(enabled):
    """Set claude_use_host_login in config/local.toml, preserving every other key."""
    marker = "claude_use_host_login"
    lines = CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else []
    rewritten, replaced = [], False
    for line in lines:
        name, separator, _ = line.partition("=")
        if separator and name.strip() == marker:
            if not replaced:
                rewritten.append(f"{marker} = {'true' if enabled else 'false'}")
                replaced = True
            continue
        rewritten.append(line)
    if not replaced:
        rewritten += [
            "# Development only: use this machine's Claude Code login when an",
            "# application has no mounted Claude credential. Refused in production.",
            f"{marker} = {'true' if enabled else 'false'}",
        ]
    CONFIG.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def ask(prompt, options):
    """One numbered choice. Returns the chosen key, or None if the person quit.

    EOF and Ctrl-C mean the same thing as an empty answer: leave everything alone.
    Console detection is not reliable enough to be the only guard - a redirected
    stdin can still look like a terminal - so refusing to guess is handled here.
    """
    while True:
        print()
        for index, (_, label) in enumerate(options, start=1):
            print(f"  {index}. {label}")
        try:
            answer = input(f"{prompt} [1-{len(options)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print("Enter one of the listed numbers, or press Enter to skip.")


def ask_secret(prompt):
    """A hidden value, or an empty string when there is nobody to ask."""
    try:
        return getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def seed_existing_applications(provider):
    """Give applications with no credential of their own a copy of the default.

    Only ever creates a missing file. An application that already has one has been
    configured deliberately - possibly with a different account - and is left alone.
    """
    source = secret_path(DEFAULT_NAMES[provider])
    seeded = 0
    for application in Application.objects.all():
        target = secret_path(f"{provider}_{application.pk}")
        if target.exists():
            continue
        write_secret(target.name, source.read_text(encoding="utf-8").strip())
        seeded += 1
    if seeded:
        print(f"Copied the credential to {seeded} existing application(s).")


def configure_claude():
    route = ask(
        "How should Claude authenticate",
        [
            ("host", "Use this machine's Claude Code login (no API key needed)"),
            ("key", "Paste an API key or setup token for this platform to use"),
        ],
    )
    if route is None:
        return False
    if route == "host":
        if settings.PRODUCTION:
            print("Refused: the host login is a development convenience only.")
            return False
        set_host_login(True)
        print()
        print("Claude will use this machine's own Claude Code login.")
        print("This is the same thing other Claude Agent SDK apps do by default.")
        print("Answers are billed to that login, not to any application, and the")
        print("chat page says so. Mount a per-application credential before sharing")
        print("this deployment - run start-all.ps1 -ConfigureAI again to do that.")
        if not (Path.home() / ".claude").exists():
            print()
            print("Note: no ~/.claude was found. Sign in once with:  claude /login")
        return True
    print()
    print("Accepted: an API key (sk-ant-api...) billing your Anthropic Console")
    print("account, or a token from `claude setup-token` (sk-ant-oat...) billing a")
    print("Claude subscription. Input is hidden.")
    value = ask_secret("Claude credential: ")
    if not value:
        return False
    if not value.startswith("sk-ant-"):
        print("That does not look like an Anthropic credential; nothing was written.")
        return False
    write_secret(DEFAULT_NAMES["claude"], value)
    set_host_login(False)
    print(f"Stored a Claude {describe(value)} in the mounted secret directory.")
    seed_existing_applications("claude")
    return True


def configure_openai():
    print()
    print("Paste an OpenAI API key (sk-...). Input is hidden.")
    value = ask_secret("OpenAI API key: ")
    if not value:
        return False
    if not value.startswith("sk-"):
        print("That does not look like an OpenAI API key; nothing was written.")
        return False
    write_secret(DEFAULT_NAMES["openai"], value)
    print("Stored the OpenAI API key in the mounted secret directory.")
    seed_existing_applications("openai")
    return True


def main():
    if not sys.stdin.isatty():
        # -Install may run from a script or a CI job. Silently doing nothing is
        # right: there is no one to ask, and no default worth guessing.
        print("Not an interactive console; skipping AI provider setup.")
        return 0
    print()
    print("=== AI provider ===")
    print("Chat, graph enrichment and Code Factory each call a model. Pick the")
    print("provider this platform should use; you can change it per application")
    print("later in AI settings.")
    for provider, name in DEFAULT_NAMES.items():
        if secret_path(name).exists():
            print(f"A {provider} credential is already configured.")
    if getattr(settings, "CLAUDE_USE_HOST_LOGIN", False):
        print("Claude is currently set to use this machine's Claude Code login.")
    provider = ask(
        "Which provider",
        [
            ("claude", "Claude (Anthropic)"),
            ("openai", "OpenAI"),
            ("skip", "Skip for now - configure it later in AI settings"),
        ],
    )
    if provider in (None, "skip"):
        print("\nSkipped. Run start-all.ps1 -ConfigureAI when you are ready.")
        return 0
    done = configure_claude() if provider == "claude" else configure_openai()
    if not done:
        print("\nNothing was changed. Run start-all.ps1 -ConfigureAI to try again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
