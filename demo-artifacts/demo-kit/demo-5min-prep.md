# 5-minute demo — defect ticket and pre-demo checklist

Engineering first (CarePathDev), one minute of Operations (CarePathOps) at the
end. Log in as **acmeadmin**. The talk track is in the outline; this file is
what to create and check beforehand.

---

## The defect ticket

Create it in Jira, project **KAN** (https://ajitk15.atlassian.net). It follows
the same shape as the seven CARE tickets already there: the CARE number goes in
the summary, and its lower-case form is a label.

It is split out of CARE-418 (KAN-6), which mixes two problems. This ticket keeps
only the small one, so it is easy to explain in a minute.

| Field | Value |
| --- | --- |
| **Summary** | `CARE-450 - Birth dates are hidden in logs as if they were phone numbers` |
| **Type** | Bug |
| **Priority** | Medium |
| **Labels** | `care-450` `privacy` `logging` `release-1.0.1` |
| **Status** | To Do |

**Description** (paste as plain text):

```
Split out of CARE-418 so it can be fixed on its own.

What happens
The phone pattern in security/phi.py is \b\+?\d[\d ()-]{8,}\d\b. It also matches an ISO-8601 date such as 1943-03-11, so redact() replaces a patient's date of birth with [phone]. Dates are redacted by accident, not by rule, and any other long number with hyphens can be mangled the same way.

Example log line from exploratory test TC-3:
Scoring readmission risk for patient 42 born [phone]

Why it matters
NFR-04 says logs must not carry protected health information, and redaction is how we meet it. A pattern that matches the wrong things hides real problems in the logs and makes them harder to read during an incident.

Where
security/phi.py, PATTERNS, the [phone] entry. Every caller of redact() is affected, including the logging filter.

Acceptance criteria
- An ISO-8601 date passes through redact() unchanged.
- Real phone numbers are still redacted.
- A test pins both cases.
- Existing redaction tests continue to pass.
```

Why this wording: the import keeps the summary, description, type, status,
priority and labels. It does **not** keep comments. Words like *NFR-04*,
*TC-3*, *phi.py*, *redact* and *logs* are what let Chat and Code Factory
match the ticket to the requirement, the test plan and the code.

---

## Pre-demo checklist

### The day before (about 30 minutes)

- [ ] **Start the platform** with `start-all.cmd`, not `runserver`, because the
      worker lanes only run under `start-all`.
- [ ] **Log in as acmeadmin** and check that both apps open: CarePathDev and
      CarePathOps.
- [ ] **Create the defect ticket** in Jira using the text above. Write down its
      KAN key here: `KAN-____`.
- [ ] **CarePathDev › Connectors › Jira › Import.** The filter is
      `project = KAN`, so the new ticket comes in. Check it appears under
      Knowledge with **Status: To Do**.
- [ ] **CarePathDev › Knowledge › Graph:** wait for the new draft to appear (the
      worker builds it on its own, with no AI), then click **Publish**. Today v3
      is published with nothing changed since, so this becomes v4.
- [ ] **Chat check.** Ask: *"What is wrong with redaction in phi.py, and which
      requirement does it break?"* The answer should cite the new ticket and
      NFR-04. If it doesn't, adjust the question until it does, and write the
      working question here: `______________________`.
- [ ] **Code Factory › Analyse a ticket:** pick CARE-450 and start the run. Wait
      until it reaches **awaiting review**. This makes one AI call. Do **not**
      approve it. Leave the existing CARE-401 run alone too.
- [ ] **Open the run** and check the evidence shows all three labels:
      **Ticket**, **Knowledge graph** and **Code graph** (`security/phi.py`).
- [ ] **Code Graph:** open `security/phi.py` and check that "used by" and
      "could reach" are filled in.

### Fix before the demo: CarePathOps is answering from an old graph

Live check on 2026-10-01: CarePathOps is answering from **v3**, but **28 of its
41 sources have changed since**. Drafts v4 and v5 exist and are not published.
Runbook passages are only cited when their source is unchanged, so most of
Part 6's runbook evidence would be silently skipped.

- [ ] **CarePathOps › Knowledge › Graph › Versions:** open the latest draft (v5),
      check its Quality tab, then **Publish** it. The banner should no longer say
      sources have changed.
- [ ] **Re-run triage on INC0010010** (*Clinic C export timing out with 504*):
      30 to 50 seconds, one AI call. Past runs are hidden when a source they
      quoted has changed, so don't rely on the old one.
- [ ] Open **Evidence → Around this incident** and check the map shows similar
      incidents, a recent change and at least one runbook passage, and that
      each row has its path line (e.g. `INC0010010 → carepath-api-green → …`).

### 30 minutes before

- [ ] **Jira:** the CARE-450 ticket is in **To Do**. Moving it is the live step.
- [ ] Check for queued or running work before you touch the server. **Don't
      restart it from here on**, because a restart kills an in-flight run.
- [ ] **Open these tabs in order:**
  1. CarePathDev › Knowledge › Graph (the graph view, published version)
  2. CarePathDev › Chat (new conversation)
  3. CarePathDev › Code Graph › `security/phi.py`
  4. CarePathDev › Code Factory › the CARE-450 run
  5. Jira › the CARE-450 ticket
  6. CarePathDev › Connectors
  7. CarePathOps › ServiceOps › INC0010010
- [ ] Zoom the browser to about 125% so the room can read it.
- [ ] Close notifications, chat apps and anything else that might pop up.

### Know before you go live

- **The Jira connector imports on its own every hour.** If it runs between you
  moving the ticket and clicking Import, the Import finds nothing new. That's
  fine: the "sources have changed" banner still shows, so carry on.
- **After you move the ticket, don't go back to the Code Factory run.** Its
  ticket evidence was checked against the To Do version, and that version has
  now been replaced.
- **If the new draft is slow to build in Part 5,** show the "1 of N sources have
  changed since" banner and explain what Publish does. Don't wait in silence.

### After the demo

- [ ] Move CARE-450 back to **To Do** in Jira (or close it), then Import again.
- [ ] Leave the CARE-450 run unapproved. Approving it opens a real GitHub pull
      request.
- [ ] Don't fix the seeded defects in `demo-artifacts/carepath`; they are the
      demo. `seeded-gaps.md` is the answer key.
