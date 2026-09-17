# Setting up the demonstration application

What to create in Digital Brain, what to load into it, and in what order.
Budget 30 minutes the first time.

## 1. Create the application

Portfolio *Healthcare*, product *Care Coordination*, application **CarePath**.

Tick these features on the create form:

| Feature | Needed for |
| --- | --- |
| Knowledge | Every act of the demo |
| Chat | Acts 1, 2 and 5 |
| Code Graph | Acts 3 and 4 — without it, design guesses at filenames |
| Code Factory | Act 4 |
| Connectors | Importing the CARE board from Jira, if you have an instance |

Tick **"Also grant me owner access"**. Creating an application does not grant
access to it — the org admin who creates it still gets nothing without an
explicit grant, and the checkbox records a real one.

`chat_api` is opt-in and off by default. Leave it off unless you are
specifically demonstrating the API surface; it is the one surface that spends
the application's provider budget.

## 2. Mount a credential

One file per provider per application, owner-only. `claude_<application id>` or
`openai_<application id>`.

`scripts/ai_setup.py` writes `<provider>_default` on a first run. That file is a
**template, not a fallback** — creating an application copies it to the
per-application name. Nothing reads `_default` at request time, so a deleted
per-application file is an error rather than a quiet borrow from another
application.

## 3. Load the sources

Order matters: load the requirements first, so that a run started early still
has the grounding documents.

| Load | Files | Why |
| --- | --- | --- |
| **First** | `docs/01-requirements/*` | Everything else cites these |
| Second | `docs/02-design/*`, including `adr/*` | The four ADRs carry the "deliberately not wrong" answers |
| Third | `docs/03-build/*`, `docs/04-test/*` | Standards, API contract, traceability matrix |
| Fourth | `docs/05-release/*`, `docs/06-operate/*` | The waiver and the incident |
| Fifth | `docs/07-govern/*` | The control matrix and the risk register |
| Sixth | `docs/00-overview/sdlc-map.pptx` | The index |
| Seventh | `demo-kit/jira-tickets.md` | Only if you are not importing from Jira |

The documents are in seven formats — Word, Excel, PowerPoint, PDF, HTML, plain
text and Markdown — because a customer's estate is never one format. Upload them
exactly as they are; the platform converts each with MarkItDown in an offline
subprocess that never sees a URL.

All 34 have been verified against `scripts/extract_text.py`, so none of them
should fail on upload. If one does, the file is corrupt rather than unsupported
— rebuild it with `tools/build_formats.py`.

**Do not load `seeded-gaps.md`.** It is the answer key, and loading it lets Chat
answer from it rather than from the project's own documents.

**Do not load `demo-script.md` or `factory-tickets.md`.** They describe the
demonstration rather than the project, and they will contaminate answers with
sentences about what a demonstration should show.

**Do not load `tools/source/`.** Those are the Markdown originals of the
documents already loaded from `docs/`. Loading both gives every source a twin,
and a citation would point at whichever was indexed first.

## 4. Generate and publish

Generate the knowledge graph, review the draft, **publish it**.

This step is not optional. `graph_snapshot` resolves to the latest *published*
revision, and no published revision is an error rather than a silent fall back
to a draft. Chat and Code Factory both go through it.

Publishing refuses a revision whose sources have moved since it was built — it
compares stored digests against current ones and declines. If it refuses,
regenerate rather than forcing it.

## 5. Register the repository in Code Graph

Push `demo-artifacts/carepath/` to a GitHub repository — its own repository, not
a subdirectory of a larger one, because Code Graph takes `owner/name` and indexes
a whole repository.

Register it as `owner/name`. Public repositories need no credential; a private
one uses the application's mounted `github_<application id>` read credential.

Wait for the snapshot. Confirm the file count and that `src/carepath/api/` shows
import relationships — that is what design reads.

## 6. The tickets

**With a Jira instance:** configure the Jira connector. Base URL, account email
in config, API token mounted as a file. JQL `project = CARE ORDER BY updated DESC`.
Import is manual, read-only, and bounded to the 100 most recently updated records.

**Without one:** load `jira-tickets.md` as a source. Each ticket is a heading;
Code Factory reads it the same way.

`jira-export.json` is the shape a live import produces — the real
`/rest/api/3/search` response, with descriptions in Atlassian Document Format.
It has been verified against the platform's own `adf_text` adapter. Use it to
show a customer exactly what would be imported, or to feed a stub.

Rebuild it with `python build_jira_export.py` after editing the ticket bodies in
that script.

## 7. Delivery credential

Only if you are demonstrating a pull request at the end of Act 4.

Mount `github_write_<application id>`. It is a **different file** from the read
credential, deliberately: granting an application the ability to import issues
does not grant it the ability to push.

## 8. Second user

Code Factory stops at `awaiting_review` and a **different** user with approve
permission has to approve. Create that user and grant them access before the
demo, not during it.

---

## Rehearsal checklist

- [ ] Application created, features ticked, owner grant recorded
- [ ] Credential mounted; a Chat question answers
- [ ] Sources loaded in order; `seeded-gaps.md` **not** among them
- [ ] Revision generated **and published**
- [ ] Repository registered; snapshot indexed; import edges visible
- [ ] Tickets available, by connector or as a source
- [ ] Second approver exists and can sign in
- [ ] Act 2's question rehearsed: "Is patient consent checked before a record is exported to a partner?"
- [ ] One CARE-401 run completed end to end, including approval
- [ ] `seeded-gaps.md` read by you, closed before the customer arrives
