# Demo setup — from an empty instance to a working Code Factory

**What this gets you:** one organization, one application, a published knowledge
graph, an indexed codebase, imported tickets, and a Code Factory run that ends in
a draft pull request with green CI.

**Time:** about 20 minutes, most of it waiting for imports and the graph build.

**What you need before you start:** four secrets, listed in
[Secrets to have ready](#secrets-to-have-ready). Everything else is in this file.

`demo-script.md` is the *narrative* — what to say and in what order once this is
done. This file is the *setup*.

---

## Secrets to have ready

Each is entered once, on the application's **Credentials** screen. None of them
is typed into a connector form, and none is ever shown again after it is saved.

| Credential | Where it comes from | Needed for |
| --- | --- | --- |
| **Jira** | id.atlassian.com → API tokens | Importing the CARE tickets |
| **GitHub (write)** | github.com/settings/personal-access-tokens | Opening the draft pull request |
| **Claude** | console.anthropic.com, or `claude setup-token` | Every model call |
| **GitHub (read)** | Only for a private repository | Nothing, if the repos are public |

The GitHub write token is **fine-grained**, scoped to `ajitk15/carepath` alone,
with **Contents: read and write** and **Pull requests: read and write**. Nothing
else. A classic `repo` token is organization-wide and is the wrong instrument.

```
JIRA_API_TOKEN      = <PASTE_YOUR_JIRA_API_TOKEN>
GITHUB_WRITE_TOKEN  = <PASTE_YOUR_FINE_GRAINED_PAT>
CLAUDE_CREDENTIAL   = <PASTE_YOUR_ANTHROPIC_KEY_OR_SETUP_TOKEN>
```

> On a development instance `claude_use_host_login = true` in
> `config/local.toml` lets runs fall back to the machine's own Claude login, so
> the Claude credential is optional there. It is **refused** under
> `mode = "production"`. Mount it before anyone else uses the instance, or every
> run bills whoever started the server.

---

## The shape you are building

```
ACME                          organization
└── Integrated Care           portfolio
    └── Care Coordination     product
        └── CarePath          application
```

| Thing | Where it lives |
| --- | --- |
| Lifecycle documents | https://github.com/ajitk15/carepathdocs/tree/main |
| The service itself | https://github.com/ajitk15/carepath |
| Tickets | https://ajitk15.atlassian.net · project KAN |

---

## 1 · Start the platform

```bash
start-all.cmd
```

Use this rather than `manage.py runserver`: the worker lanes that run imports,
graph builds and Code Factory only start under `scripts/serve.py`, which is what
`start-all` launches. Under `runserver` a queued run sits at "Queued" forever.

Sign in at http://127.0.0.1:8000 with the administrator account. Pass `-Port` to
`start-all.ps1` if that port is taken.

---

## 2 · Create the application

One form makes all four levels. Go to **Overview → the organization → New
application**, or straight to:

```
/organizations/<ORG_ID>/applications/new/
```

| Field | Value |
| --- | --- |
| Portfolio | *leave blank* |
| New portfolio name | `Integrated Care` |
| Product | *leave blank* |
| New product name | `Care Coordination` |
| Application name | `CarePath` |
| Application owner | yourself |
| Explicitly grant this owner Code Factory approval rights | **tick** |

**Tick these features:** Knowledge, Code Factory, Code Graph, External
connectors. Leave **Chat API** unticked — it is opt-in because it spends the
application's model budget through an API surface.

Submitting lands you on the **onboarding checklist**, which is the map for
everything below. Work top to bottom; it always shows where you got to.

---

## 3 · Credentials

**Settings → Credentials.** Add **Jira**, **GitHub (write)**, and **Claude** from
the table above.

Each is written to a file with owner-only permissions. The database keeps only
who set it, when, and a digest — never the value.

---

## 4 · Knowledge: load the documents and publish a graph

**Knowledge → Add from a link:**

```
https://github.com/ajitk15/carepathdocs/tree/main
```

The `/tree/` form is what walks the directory. A plain repository URL imports the
README and nothing else — which looks like success and is the most common way to
get this wrong.

Twenty-seven documents in seven formats download and convert in the background.
Wait for the rail to settle. One or two occasionally fail; press **Retry** on the
row and they convert.

Then **Generate graph → Structural only → Publish**.

- Structural is free and deterministic; no model is called.
- **Publish it.** A draft answers nothing: Code Factory refuses to run without a
  published graph, because every item it produces has to cite evidence a
  reviewer can open.
- Expect roughly 2,700 nodes and 2,900 edges from these documents.

---

## 5 · Code Graph: index the service

**Code Graph → Add repository:**

```
ajitk15/carepath
```

Public, so no credential is needed to index it. The clone reads the source and is
deleted; the code is never executed.

> **Index exactly one repository on this application.** With two, a run cannot
> tell which one a ticket means, declines to guess, and silently reasons with no
> code at all — the design phase then names components like "the consent
> service" instead of `src/carepath/services/consent.py`, and implementation has
> nothing to open. If you have registered others, remove them from the Code
> Graph screen.

---

## 6 · Connectors: import the tickets

**Connectors → New connector → Jira.**

| Field | Value |
| --- | --- |
| Site URL | `https://ajitk15.atlassian.net` |
| Authentication | Jira Cloud (account email + API token) |
| Account email | `ajitk15@gmail.com` |
| JQL filter | `project = KAN ORDER BY updated DESC` |

The token is **not** entered here — it is the Jira credential from step 3.

Press **Sync**. Seven CARE tickets arrive. Status, priority and labels are part
of the imported body, so a ticket moving to Done reads as a changed record rather
than an identical one.

---

## 7 · Confirm the checklist is green

Open **Code Factory → Onboarding**. The analysis gate should read **Ready**:

```
Analyse a ticket ......................... Ready
  ✓ Code Factory switched on
  ✓ Knowledge graph published
  ✓ Model configured for plan drafting
  ✓ Provider credential mounted
  ✓ Tickets imported
```

The delivery gate will still want **someone to approve other than the author** —
see [Running it single-handed](#running-it-single-handed).

---

## 8 · The run

**Code Factory → Analyse a ticket → CARE-401** (consent not checked before
export) **→ Analyse.**

You land on the run screen. Every stage is one collapsible line:

| Stage | What happens |
| --- | --- |
| **1 Pre-checks** | Six green ticks, before anything is spent |
| **2 Analysis** | Live, step by step: the graph it connects to, the evidence verified, the gaps found |
| **3 Gaps** | ~8 gaps, all ticked. Click one for a popup with its evidence. Untick what you do not want, add a review note, **Approve** |
| **4 Implementation agents** | Work order → Implementation → Test author → Change review → Pre-write checks, each reporting what it did |
| **5 Summary** | The files, new vs modified, **before anything is written** |
| **6 Pull request** | Yes opens a draft; No discards it and nothing was written |
| **7 Tests** | `carepath`'s own CI runs on the branch; this platform never runs code |

The whole run takes two to three minutes, mostly model calls.

### What to point at

- **Untick a gap and approve.** The unticked one is marked rejected, nothing is
  written for it, and the audit record says 6 of 8 were accepted.
- **Stage 5 before stage 6.** The change exists, in full, and has touched
  nothing. "No, discard it" is a real answer.
- **Stage 7.** The test author agent wrote the tests; GitHub ran them. The
  platform never executed a line of the customer's code.

---

## Running it single-handed

Whoever starts a run authors its plan, and an author may not approve their own
work. On a one-person instance the pipeline therefore cannot finish.

Two ways out:

1. **A second account.** Give a second user Code Factory approval on CarePath.
   Start runs as one, approve as the other. Nothing to configure.
2. **`allow_self_approval = true`** in `config/local.toml`. `load_config`
   **refuses** it under `mode = "production"`, the screen says you are reviewing
   your own plan, and the audit record carries `self_approved: true`.

---

## Client environments with no outbound GitHub write

Leave the **GitHub (write)** credential unset. Stage 4 then reads the pinned
Code Graph snapshot instead of the live repository, writes the files locally, and
stops at the summary. Nothing reaches GitHub, the repository confirmation is not
asked for, and stages 6 and 7 do not appear.

The staleness check is reported as **impossible** rather than skipped, because
the files came from a snapshot and not from a live branch.

---

## When something does not work

| Symptom | Cause |
| --- | --- |
| Run stuck at "Queued" | Started with `runserver`; no worker lanes. Use `start-all.cmd` |
| Only the README imported | The link was the repository root. Use the `/tree/main` form |
| "No knowledge graph is published" | A graph exists but was never published. Publish it |
| Design names components, not files | No snapshot pinned — no repository indexed, or more than one |
| A document failed to convert | Press **Retry** on the row |
| "This plan did not record the digest of its evidence" | The plan predates the current build. Run the ticket again |

Every run keeps its own record. Open it from **Code Factory → Full run history**
and read it back step by step exactly as it read while it was running — which is
also the fastest way to answer "what did it actually do?" in front of an
audience.
