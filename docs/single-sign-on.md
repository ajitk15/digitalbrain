# Single sign-on with Microsoft Entra ID

Digital Brain can offer **Sign in with Microsoft** beside the password form. It
uses OpenID Connect (authorization code flow with PKCE) against **one** Entra ID
tenant. Code: `platform_core/sso.py`; tests: `platform_core/tests/test_sso.py`.

Signing in **proves who someone is and grants nothing**. A Microsoft sign-in
never adds an organization membership or an application grant; those are still
given on the Access screens. Entra groups and app roles are not read.

## 1. Register the application in Entra ID

In the [Entra admin centre](https://entra.microsoft.com) → **App registrations**
→ **New registration**:

| Field | Value |
| --- | --- |
| Name | Digital Brain (anything you like) |
| Supported account types | **Accounts in this organizational directory only** (single tenant) |
| Redirect URI | Platform **Web**, `https://<your host>/accounts/sso/callback/` |

For local testing the redirect URI is `http://localhost:8000/accounts/sso/callback/`
(Entra accepts plain `http` only for `localhost`). Use exactly the host and port
you open in the browser - `127.0.0.1` and `localhost` are different URIs to Entra.

Then:

1. **Overview** - copy the **Application (client) ID** and **Directory (tenant) ID**.
2. **Certificates & secrets** → **New client secret** - copy the *Value* (shown once).
3. **API permissions** - the default `User.Read` delegated permission is enough;
   `openid` and `profile` are requested at sign-in. Grant admin consent if your
   tenant requires it.
4. Optional: **Enterprise applications** → this app → **Properties** →
   *Assignment required?* **Yes**, then assign the users or groups allowed to
   sign in. Everyone else is refused by Microsoft before they reach this server.

## 2. Configure Digital Brain

In `config/local.toml` (or the production config):

```toml
entra_tenant = "<Directory (tenant) ID>"
entra_client_id = "<Application (client) ID>"
# Optional: give someone the tenant knows, but this platform does not, an
# account with no access on their first sign-in. Off unless written down.
# entra_create_users = true
```

Put the client secret value in a file named `entra_client_secret` in the
`secret_directory` (owner-only permissions on Linux, like every other mounted
secret). It is never a configuration field - `load_config` refuses one.

Both ids must be GUIDs. `common`, `organizations` and `consumers` are refused:
the sign-in is pinned to one directory, which is what makes linking by sign-in
name safe.

Restart the server. The sign-in page now shows **Sign in with Microsoft**.

## 3. Who signs in as whom

| Situation | What happens |
| --- | --- |
| Microsoft account already linked | Signed in as the linked user, matched by object id (`oid`). A later rename in Entra changes nothing. |
| First sign-in, one user here whose username or email equals the Entra sign-in name (UPN) | Linked to that user, audited `sso_linked`. |
| That user is a **platform administrator** | Refused. An operator links it with `manage.py link_sso`. |
| That user is already linked to a different Microsoft account | Refused. |
| More than one user matches | Refused - the server does not guess. |
| Nobody matches, `entra_create_users` off (default) | Refused: "no account here yet". |
| Nobody matches, `entra_create_users = true` | A new user with no password, no organization and no grants; audited `sso_user_created`. |
| Linked user is deactivated | Refused. |

Link or unlink explicitly (the object id is on the user's **Overview** page in Entra):

```bash
.venv/Scripts/python.exe manage.py link_sso <username> --object-id <Entra object id>
.venv/Scripts/python.exe manage.py link_sso <username> --clear
```

Password sign-in keeps working for everyone with a password, including the
bootstrap administrator.

## 4. How to test it

**Without Azure (always):** `manage.py test platform_core.tests.test_sso`. It
runs a fake Microsoft that signs real RS256 tokens with a key generated in the
test and publishes the public half as the tenant's key set, so signature,
issuer, audience, tenant, expiry, nonce, state and PKCE are all checked for
real. Each refusal test breaks exactly one of them.

**Against a real tenant:** any Entra tenant works, including the free one that
comes with an Azure free account or the Microsoft 365 Developer Program.

1. Register the app as in step 1 with redirect URI
   `http://localhost:8000/accounts/sso/callback/`.
2. Set `entra_tenant` / `entra_client_id`, write `entra_client_secret`, restart.
3. Create a user here whose username or email equals a test user's UPN (e.g.
   `tester@yourtenant.onmicrosoft.com`), or set `entra_create_users = true`.
4. Open `http://localhost:8000/accounts/login/`, press **Sign in with
   Microsoft**, sign in as that test user. You land on the dashboard; the user
   holds no applications until granted.
5. Check the refusals that matter: an Entra user with no account here (refused),
   a platform administrator (refused until `link_sso`), and pressing **Cancel**
   on Microsoft's page ("it was cancelled").

A refused sign-in logs `{"event": "sso.refused", "failure_kind": "<label>"}`
with a fixed label (`state`, `nonce`, `claims`, `unknown`, `admin_by_name`...)
and nothing from the request or the token.

## What is deliberately not here

- **Mapping Entra groups or app roles to grants.** That would let the directory
  widen access without an audited grant here; it needs its own decision.
- **Single sign-out.** Signing out ends this session only, not the Microsoft one.
- **Requiring SSO / disabling passwords.** Password sign-in stays on.
- **Multi-tenant sign-in.** Refused by configuration.
