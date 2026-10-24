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

## 5. Provisioning accounts from Entra (SCIM)

Sign-in links an account that already exists. Provisioning makes Entra create
it, keep its name and email current, and **deactivate it the moment someone is
unassigned or leaves** - so access ends with the directory, not whenever
somebody remembers. Code: `platform_core/scim.py`; tests:
`platform_core/tests/test_scim.py`.

**It creates accounts and grants nothing.** A provisioned account has no
password, no organization and no application grant; it signs in with Microsoft
and sees nothing until someone grants it access on the Access screens. Groups
are refused. A **platform administrator is never matched, created over or
changed** - an operator manages those, as with `link_sso`. Nothing is ever
deleted: removal is deactivation, which signs the person out at once and stops
their API tokens and connector schedules.

**Entra must reach the server over the internet**, so this is set up on the
hosted deployment (or behind a tunnel), never against `localhost`.

### Turn it on

The deployment's environment (compose):

```yaml
DIGITAL_BRAIN_ENTRA_TENANT: "<Directory (tenant) ID>"
DIGITAL_BRAIN_ENTRA_CLIENT_ID: "<Application (client) ID>"
DIGITAL_BRAIN_SCIM: "1"
```

or, in a configuration file, `scim_provisioning = true` beside the two Entra
ids (`load_config` refuses it without them - an account with no password and no
Microsoft sign-in could never be used). Then mount two secret files, mode 0600:

| File | Value |
| --- | --- |
| `entra_client_secret` | The client secret from step 1 |
| `scim_token` | A random token of at least 32 characters, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |

Restart. The entrypoint warns at start while either file is missing.

### Set up provisioning in Entra

1. **Enterprise applications** → **New application** → **Create your own
   application** → *Integrate any other application you don't find in the
   gallery (Non-gallery)*. (The app registration from step 1 handles sign-in;
   provisioning is configured on an enterprise application.)
2. **Provisioning** → **Get started** → *Provisioning Mode* **Automatic**:
   - *Tenant URL* `https://<your host>/api/scim/v2`
   - *Secret Token* the contents of `scim_token`
   - **Test Connection** - it asks for a user that does not exist and must be
     told "none"; nothing is written.
3. **Mappings**:
   - *Provision Microsoft Entra ID Groups* → **Enabled: No**. Groups are
     refused here by design.
   - *Provision Microsoft Entra ID Users*: keep `userPrincipalName → userName`
     (the sign-in name, which is how Microsoft sign-in links the account),
     `Switch([IsSoftDeleted]...) → active`, `givenName → name.givenName`,
     `surname → name.familyName`, `mail → emails[type eq "work"].value`.
     Everything else may stay or go - attributes this platform does not keep
     are ignored.
4. **Users and groups** → assign the people (or a group, whose members are
   provisioned one by one - the group itself is not sent).
5. *Scope* **Sync only assigned users and groups**, then **Start provisioning**.
   Entra runs a cycle about every 40 minutes; **Provision on demand** pushes one
   person at once, which is the quickest test.

### What happens to whom

| In Entra | Here |
| --- | --- |
| Assigned, no account here | Account created, audited `account.provisioned` |
| Assigned, one account here whose username or email is the UPN | Linked, audited `account.directory_linked`; a password it had is kept |
| Assigned, and it is a platform administrator | Refused (409) - never managed by the directory |
| Name, email or UPN changed | Copied over, audited `account.updated` (field names, never values) |
| Unassigned or blocked | Deactivated, audited `account.deprovisioned`, which names any organization or application left with no active administrator or owner |
| Re-assigned | Reactivated, `account.reprovisioned` |
| Deleted | Deactivated and no longer the directory's, `account.directory_unlinked`; never deleted |

Every event is recorded as the non-login account `entra-provisioning`. Test it:
provision one person on demand, sign in as them with Microsoft (they see nothing),
grant them an application, then unassign them and watch their next page load go
to the sign-in screen.

## What is deliberately not here

- **Mapping Entra groups or app roles to grants.** That would let the directory
  widen access without an audited grant here; it needs its own decision.
  Provisioning (section 5) creates and deactivates accounts and refuses groups
  for the same reason.
- **Single sign-out.** Signing out ends this session only, not the Microsoft one.
- **Requiring SSO / disabling passwords.** Password sign-in stays on.
- **Multi-tenant sign-in.** Refused by configuration.
