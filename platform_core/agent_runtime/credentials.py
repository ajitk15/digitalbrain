"""Mapping a mounted Claude credential onto the environment the CLI expects.

The Claude Agent SDK runs the bundled Claude Code CLI, and that CLI resolves
credentials in a fixed order: `ANTHROPIC_API_KEY`, then `CLAUDE_CODE_OAUTH_TOKEN`,
then a stored interactive login in `CLAUDE_CONFIG_DIR`. This platform never uses
the third: the sandbox points `CLAUDE_CONFIG_DIR` at an empty directory precisely
so one application can never inherit another's session, or the server operator's.

That leaves two kinds of credential an owner may mount, and they are NOT
interchangeable — each is read from its own variable:

* An **API key** (`sk-ant-api…`), which bills the Console account.
* An **OAuth token** from `claude setup-token`, which bills a Claude subscription
  and works headless, with no interactive login on the server.

Both live in the same per-application secret file, so isolation, rotation and the
rest of the secret handling are unchanged. Only the variable differs, and the file
contents decide which.
"""

#: Tokens minted by `claude setup-token` carry this prefix.
OAUTH_PREFIX = "sk-ant-oat"

#: Variables the CLI needs in order to find a login stored on this machine. In the
#: normal path these are redirected at an empty directory; under host login they
#: are left alone so the real ~/.claude is visible.
HOST_LOGIN_PASSTHROUGH = {
    "HOME",
    "USERPROFILE",
    "CLAUDE_CONFIG_DIR",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
}


def is_oauth_token(credential):
    return str(credential).strip().startswith(OAUTH_PREFIX)


def describe(credential):
    """A non-secret label for logs and operator-facing messages."""
    if not str(credential or "").strip():
        return "this machine's Claude Code login"
    return "OAuth token" if is_oauth_token(credential) else "API key"


def host_login_enabled():
    """True when this deployment may fall back to the operator's own Claude login."""
    from django.conf import settings

    return bool(getattr(settings, "CLAUDE_USE_HOST_LOGIN", False))


def claude_environment(credential):
    """The auth variables for one Claude run.

    The unused variable is blanked rather than omitted so a stale value can never
    survive into the subprocess and silently pick the wrong account.
    """
    value = str(credential or "").strip()
    if not value:
        # Host-login mode: supply no credential at all and let the CLI fall through
        # to the stored session. Both variables are omitted, not blanked, because an
        # empty value is still a value to the CLI.
        return {}
    if is_oauth_token(value):
        return {"CLAUDE_CODE_OAUTH_TOKEN": value, "ANTHROPIC_API_KEY": ""}
    return {"ANTHROPIC_API_KEY": value, "CLAUDE_CODE_OAUTH_TOKEN": ""}
