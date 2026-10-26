# Industry domain in Settings - plan

The industry domain (`Application.ontology`, `ontology.PACKS`: core, US insurance,
healthcare) decides what AI graph extraction looks for. Owners asked for it in
Settings, with the consequence of changing it said before it happens, and for
CarePath to move to Healthcare with its graph regenerated.

## What is there today

- **Chosen at creation** - `CreateApplicationForm.ontology` (`forms.py`), saved by
  `views.create_application`, audited in `application.created`.
- **Already changeable, but hard to find.** `graphs.set_ontology` is an owner-only
  form at the *bottom* of the Generate graph popup (`graph_generate.html`,
  `#industry-domain`, `action=ontology`). It is below the Generate button in a popup
  most owners open to generate, needs the Knowledge feature on, and nothing in
  Settings, onboarding or the cycle links to it. The cycle's Set up stage says
  "Healthcare ontology" and links to **Features**, which cannot change it.
- **What a change does now:** writes the field, audits `graph.ontology_changed`
  (from/to), calls `request_refresh`. A pack is part of `graphs.fingerprint`
  (`rubric(domain)`), so the refresh sees a moved fingerprint and
  `refresh_graph` → `arm_auto_refresh` queues an incremental AI run when
  `automatic_ai` allows it. The message only says, *after* the change, that
  charges apply.

## What is wrong with it

1. **A change regenerates only part of the graph, then stops.** The extraction
   cache is keyed by rubric (`graph_ai.rubric`: `ontology-N-healthcare-V`), so after
   a change no window is cached under the new domain, and `reusable` refuses a
   version from another pack. One AI run makes at most `MAX_EXTRACTION_SOURCES`
   (25) calls. CarePath has more windows than that (about 43 sources, some read in
   several 24,000-character windows). The first run reads 25 windows and saves a
   version, and the fingerprint then matches, so **no further run happens** until
   a source changes or someone presses Generate again, and again. Meanwhile the
   new version carries *fewer* AI relationships than the Core one did, because
   nothing is carried across packs. This is the main gap. It also affects a
   large first upload, for the same reason.
2. **Nothing is said before the change.** Not the cost, not whether the AI run will
   happen (automatic AI off, no credential, budget spent → a structure-only
   version), not that the published version keeps answering until someone
   publishes, not that switching back is free (answers under the old rubric
   stay cached).
3. **The release check doesn't explain itself.** The first Healthcare version is
   checked against a published Core version: "AI took answers away" and "share of
   proposals refused" can fail, and the report never says the two versions follow
   different domains. Holding the version back is right, because a partly read
   version should not auto-publish. Not saying why is the gap.
4. **Healthcare refuses unmasked patients** (`masked_types`: `Patient` only as
   `[MRN-…]`). With masking off, proposals naming patients are refused and counted,
   which raises the refused share. That is correct behaviour, but the page should
   say it: switching on masking (AI settings → Personal data) helps.

## Plan

### 1. Settings → Application → Industry domain (new screen)

- Route `applications/<uuid:pk>/domain/`, name `application-domain`, view
  `graphs.domain_settings`, owner-only through `application_for(owner=True)`
  (a member who is not an owner gets 403 and someone with no grant 404, as on the
  other owner screens). Listed in
  `settings_sections` after Features, grouped `"Application"` in
  `SETTINGS_GROUPS`, framed by `{% settings_page %}`. A real `<form method="post">`
  with no JavaScript, so no `data-modal`, no CSP change, and no browser test
  needed.
- **One change function** for both entry points: move the body of
  `graphs.set_ontology` into `graphs.change_ontology(user, app, key)` returning
  an outcome (changed / unchanged / refused-in-flight / unknown). The settings view
  and the Generate popup's old `action=ontology` POST both call it, and that POST
  keeps working, like the Features page's old single-toggle form.
- **The page leads with the current domain**, then the radio cards
  (`purpose-cards`, as today), then a **warning panel** (`notice notice-warning`)
  shown before anything is saved. Worked out by a new
  `graphs.ontology_impact(app, key)`, with no model call:
  - *The graph will be regenerated.* "Relationships extracted under Core are not
    carried over. Every source is read again under Healthcare: N passages in M
    sources, K of them already read under Healthcare before (free)." N and K come
    from `graph_ai.windows` and `cached_answers(..., domain=PACKS[key])`.
  - *Cost.* "About C model calls in R runs (25 a run), at most $X at the prices
    entered for <model>." That is the gateway's own reservation arithmetic: prompt
    characters ÷ 3 plus the reply cap, at the `AIConfiguration` rates. If the model
    is not priced, say so, as the Prices table does.
  - *Who pays and whether it will run* - `automatic_ai(app, graph)`: "charged to
    <payer>, within the monthly budget", or its reason ("Graph generation is
    switched off in AI settings…"), in which case: "the graph is rebuilt from
    structure now; AI relationships return when someone generates with a model".
  - *What keeps answering.* "Chat and Code Factory keep answering from the
    published version (vN) until a new one is published. Versions built under
    Healthcare are drafts, checked against it." Mention auto-publish when it is on.
  - *What doesn't change.* Code Factory and ServiceOps still use the software
    delivery types. Switching back later costs nothing, because answers already
    paid for are kept.
  - Healthcare only: the masked-patient note above, with a link to Personal data.
- **Confirmation is a required checkbox**: "I understand the graph will be
  regenerated and provider charges apply". It is checked on the server, so it
  works with JavaScript off. Lighter than the typed-name delete, because this is
  reversible.
- While a generation is queued or running, the form is disabled with the reason
  (same `run_in_flight` rule `set_ontology` already enforces on POST).
- After saving: back to the page, success message, a "Watch it build → Knowledge
  → Versions" link, and a progress line (see 2).

### 2. Regenerate to the end

After an AI run's version is saved (`graphs.rebuild`), if **all** of these hold:
the run stopped at the call cap (not the budget, a failure, or unreadable replies
only), at least one new answer was paid for in it (so a chain always ends),
windows remain with no cached answer under the current rubric, and
`automatic_ai` still says on, then **arm another automatic run**
(`arm_auto_refresh` with the same payer, audited `automatic`, after a short
`request_refresh` delay). Each step is a draft version, and every paid window is
cached, so nothing is paid for twice and a stopped chain resumes where it left
off. `graph_ai` records `quality["unread_windows"]` on each version so the
settings page, the Quality notes and the run's own message say "read under
Healthcare: 25 of 61 passages". No migration: the remaining count is computed
from the cache.

**Decided:** chain after *every* run that stops at its call limit, since a large
first upload has the same gap. No new column.

### 3. Say it everywhere it is read

- Generate popup: replace the bottom form with one line, "AI extraction follows
  **Healthcare** · Change in Settings", linking to the new screen.
- Cycle Set up stage (`lifecycle.stages`): **not changed**. Its one link opens
  Features, which is where the application's purposes are set, and Industry domain
  sits beside Features in the same Settings group.
- Create form help text: "an owner can change it later in Settings → Industry
  domain".
- Release report (`release.check`): when `data["ontology_pack"]` differs from the
  published version's, add an informational line (not a check, and no rule is
  relaxed): "Built under Healthcare; the published version under Core, so its AI
  relationships are not comparable one for one." The `ontology` soft check is
  unchanged.
- Quality notes on the version: "Built under Healthcare (changed from Core on
  <date>)."

### 4. Tests (`test_ontology_packs.py`, `test_release.py`)

Settings screen owner-only (404 for contributor and viewer) and listed only for
owners; the confirmation checkbox is required; refused while a run is in flight;
audited and refreshed; the impact counts windows already cached under the target
pack as free and reports the `automatic_ai` reason when off; the old Generate POST
still works; the chain continues while capped with progress and stops on no
progress, a budget refusal, automatic AI off, or nothing left; the release report
names a pack change. CLAUDE.md: extend "AI extraction is typed by ontology.py"
with the screen and the chain.

### 5. CarePath to Healthcare (operational, after 1-4 pass the five gates)

1. Start Docker (`digitalbrain-dev`: PostgreSQL is not answering now) and check no
   Code Factory run is in flight before restarting the server for the new code.
2. As CarePathDev's owner, Settings → Industry domain → Healthcare. Read the impact
   panel (calls and dollar ceiling) before confirming.
3. Watch the chain on Knowledge → Versions until "unread" is 0. Confirm each
   version's `GraphRevision.model` is the model rather than structural.
4. Read the release check, compare Quality's with/without-AI figures against the
   Core version, and publish only if they hold.
5. CarePathOps (Operations only, "Runbooks"): same steps, if wanted. Its triage
   reads the published graph, and the operations graph rebuilds itself when the
   published version changes.

The scorecard measures the core registry and should not move. A Healthcare probe
for CarePath (typing labelled clinical entities) is worth adding later, before
anyone relies on the pack's numbers.
