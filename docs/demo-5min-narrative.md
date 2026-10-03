# 5-minute demo narrative: one ticket, from knowledge to code

**Audience:** leadership
**Length:** 5 minutes, about 600 words spoken
**Application:** CarePathDev (Engineering), then CarePathOps (Operations). Log in as **acmeadmin**.
**Live actions:** none. Nothing is generated or run on stage: a full run takes about 15 minutes.
Everything shown was built beforehand, all on this machine and none of it on GitHub: the published
knowledge graph (v3), the Code Graph of the `carepath` folder, and Code Factory run 1 on CARE-401
(KAN-4), which wrote its fix into the folder, passed the project's own tests (115 passed) and
refreshed both graphs.

| # | Part | Time | Screen |
|---|---|---|---|
| 1 | Opening | 0:00–0:30 | Knowledge › Sources |
| 2 | The ticket | 0:30–0:45 | Jira, CARE-401 |
| 3 | Graph traversal | 0:45–1:30 | Knowledge › Graph |
| 4 | Code Factory | 1:30–3:45 | Code Factory › Run 1, its plan, and its before and after |
| 5 | ServiceOps | 3:45–4:40 | CarePathOps › ServiceOps › INC0010010 |
| 6 | Close | 4:40–5:00 | (stay on ServiceOps) |

---

## 1. Opening (0:00–0:30)

**Screen:** CarePathDev › Knowledge › Sources

> In the interest of time, I won't generate the graphs or run a defect fix live. Instead, I'll walk
> you through a pre-run example to show how the knowledge graph supports the entire process.
>
> We collected our project documents and imported Jira tickets through connectors. The documents
> are converted into Markdown and used to build the knowledge graph.

What the screen shows: the `docs` folder with 27 files, and the seven CARE tickets, each marked as
used by graph v3.

## 2. The ticket (0:30–0:45)

**Screen:** Jira, CARE-401 (KAN-4). Show the title only.

> Let's take one example: *"Patient consent is not checked before a record is exported to a
> partner."* That's the defect captured in this Jira ticket. To fix it, we need to understand the
> business requirement, the gap found during testing, and the code involved.

## 3. Graph traversal (0:45–1:30)

**Screen:** CarePathDev › Knowledge › Graph

**Click:** type `T-03` 

> Let me show how graph traverse, 

> Here's one test finding T-03 show
>    the business requirement it affects

>    who owns it

>    Severity

>    and the source documents.

> The graph brings that context together in one view, also it links that finding to the actual code files:

>    the consent service,

>    the export routes and its tests.

What the screen shows: T-03 in the middle with *Requirement: BR-05*, *Owner: Engineering* and
*Severity: High*, and six code files as blue squares: `consent.py`, `routes_consent.py`,
`errors.py`, `test_consent.py`, `routes_fhir.py` and `test_fhir.py`. The inspector gives each link's
proof: the document line, the code line and the snapshot. Links are by a shared identifier (BR-05),
not a claim that the code implements it - the inspector says so.

## 4. Code Factory (1:30–3:45)

**Screen:** Code Factory › Run 1 · KAN-4

> Now let's look at how Code Factory handled this ticket. This run was done earlier, but let me walk
> you through each stage and how the knowledge graph helps.

**Pre-checks** (point at *Pre-checks: All 7 passed*)

> First, it makes sure it has what it needs: a knowledge graph, the code graph, and the ticket.
> All checks passed.

**Analysis** (open the plan, point at the two lines at the top: *published graph version 2* and
*carepath, Code Graph snapshot v1*)

> In analysis, it first reads the ticket to understand what's being asked.
> Then it pulls the connected, verified facts from the knowledge graph and the relevant code, compares them to find
> the gaps, and designs a fix for each one.

**One gap** (open *Export path has zero consent enforcement today*)

> Let's look at one of the gaps it found, mainly there are four sections under each gap
>
> - **under the The gap it stated that** the export checks permissions never calls the consent service, so a patient's "no" is ignored.
> - **under the changes it states** the export must ask the consent service before sending anything.
> - **under the Files section** it stating out what files is part of this gap fix 
> - ** Evidence was pulled**  from the knowledge graph, by checking word for word against its source.
>
> A person reviewed and approved the plan.

**Implementation** (back to the run, point at *Implementation agents*, then *Write to folder* and
*Tests*)

> Once approved, the implementation agents take over. They turn each gap into a change, write the
> code and tests, review the result, and re-check the files before anything is written.

>  A person then review and approve, and the fix was written into the project folder - nothing was committed.

**Refresh** (open *Refresh*, point at the two drawings, then click one to enlarge)

> Finally, it refreshes the knowledge and code graphs, so the next ticket starts from up-to-date knowledge.
> Here is the before and after. Before, the export route had no link to the consent
> service, and tests pointed only at the consent code. After, in green: the export now calls the
> consent service, and the requirement and the test finding are linked to the export route and its
> tests.

## 5. ServiceOps (3:45–4:40)

**Screen:** CarePathOps › ServiceOps › INC0010010 → *Evidence behind this triage* → *Around this
incident*

> The same knowledge also helps when something breaks in production. Here's an incident: *Clinic C
> export timing out.*
>
> Starting from the incident, the graph walks out to similar past incidents and how they were
> fixed, recent changes to the same service, and the relevant runbook pages from the same knowledge
> graph. Each item shows how it was found, so the on-call engineer can trust it and act faster.

## 6. Close (4:40–5:00)

> So one knowledge graph supports the whole lifecycle: from a Jira ticket, to the requirement, to
> the code fix, to production support. And every step is traceable to its source.

---

## If you are asked

**How does Code Factory connect to the knowledge graph?** There's no API in between. They run in
the same platform: a run pins the published graph version, and at each stage it reads that version
from the database, finds the facts that match the ticket, follows their links one step out, and
sends only quotes it has verified against the source to the AI.

**What does the analysis stage do in the backend?** Three AI steps: triage reads the ticket; gap
analysis pulls verified evidence from the knowledge graph and the pinned code, then compares what
is asked for with what exists; change design names the files to change. The plan then waits for a
person. Nothing is written until someone approves.

**Does it need GitHub?** No. This demo is entirely local: the code is a folder on this machine, the
agents read its snapshot, and the fix is written back into it. GitHub works side by side - a
repository from GitHub gets a draft pull request and its own CI instead.

**Does it write code without review?** No. A person approves the plan, a person says yes before
anything is written, and nothing is committed: the folder's own git decides what happens next.

**Does it run the code?** Only when asked. A folder has no CI, so an approver can press *Run the
tests*; it runs the project's own test suite with the project's own environment and records the
result. That is switched off in production, where the repository's CI runs the tests.

**Why does T-03 still read as an open finding?** The graph says what the documents say, and the
traceability matrix has not been updated. The code side shows the fix; closing CARE-401 in Jira or
updating the matrix is what changes the knowledge itself.

**What about several applications?** One platform, many applications. Each one has its own
knowledge graph versions, code, Code Factory runs, credentials and AI cost, and access is checked
on every request, so one application never sees another's knowledge.

---

## Before you present

- [ ] Server running with `start-all.cmd`. Do not restart it while a Code Factory run is in flight.
- [ ] Knowledge › Graph shows **Version 3 PUBLISHED**.
- [ ] **Leave `demo-artifacts/carepath` as it is.** It holds run 1's fix, and the code graph, the
      before-and-after and the 115 passing tests all describe it. Restore it with
      `git checkout -- demo-artifacts/carepath` only to rehearse a fresh run, then reset ACME and
      re-index the folder.
- [ ] Code Factory run 1 (KAN-4) shows all seven stages green: *Write to folder* done, *Tests*
      showing *115 passed*, and *Refresh* open on the two drawings.
- [ ] In the graph, select the **second** *record: T-03* (traceability matrix). Rehearse the pick.
- [ ] On CARE-401 in Jira, show the title and labels only. The ticket body names a demo patient and
      their medical record number.
- [ ] Don't search or zoom near the patient name in the knowledge graph either.
- [ ] **CarePathOps needs rebuilding for section 5:** import its ServiceNow incidents and changes
      and the automation list, generate and publish its graph, then run triage on INC0010010.
      Confirm what *Around this incident* shows before relying on it.

## Backup clips

Silent recordings in `outputs/demo-clips/`, to play if something fails live:

- `clip-2-gap-to-code-walk.webm`: the graph walk from the test finding to the code. Recorded
  before **Expand related code** existed. Re-record it with the new view.
- Code Factory clip: not recorded yet. Record run 1's stages and the before-and-after.
- ServiceOps clip: not recorded yet.
