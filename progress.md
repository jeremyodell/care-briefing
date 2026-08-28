# Progress Tracker

## Active: Care Briefing — AI Prototype Challenge (Healthcare scenario)
Time-boxed consulting-firm evaluation: build a working AI prototype (120 min
recommended) + one pitch slide (10 min) + live demo/defense (20 min).
Scenario chosen: **Healthcare** — "structured briefing for a given medical
condition: current standard of care, emerging treatments in development, key
companies/institutions involved." Audience: a health system strategy team.

### Status
**Design approved in chat, not yet built.** No corpus files or app code
exist yet. `CareBriefing-Progress-Status.html` (internal build-status page,
Allata dark-theme house style, opened in browser for Jeremy) has been added
alongside this progress.md — visual tracker only, not part of the
deliverable itself. ~1 hour left on Jeremy's self-imposed build clock as of
this update; still waiting on his "go" to start the corpus/app build.

### Key Decision: reuse towncrier's harness pattern, not build from scratch
Sibling project `DEV/apps/jeremy/towncrier` is a city-agnostic civic advisor
harness (crew of agents, cited git-versioned corpus, cite-or-decline Advisor,
self-contained-HTML Publisher). The healthcare ask is structurally the same
shape — synthesize scattered public sources into a structured, cited answer —
so this project strips towncrier down to its two highest-value roles instead
of reinventing:
- **Researcher** (adapted from `towncrier/crew/advisor.md`): cite-or-decline
  discipline — answers only from the loaded corpus, declines rather than
  fabricating when a claim or condition isn't covered.
- **Publisher** (near-verbatim `towncrier/crew/publisher.md`): renders the
  approved briefing as one self-contained HTML file, inline CSS, no build
  step — stretch goal if time allows.
Pitch narrative: "we adapted a production-shaped harness we already run for
a city-records client, to a new domain, in under two hours."

### Decisions Made (this session)
- **Data model: pre-loaded corpus, not live web search.** 3-4 markdown files
  (one per condition), each with frontmatter + 3 cited sections (Standard of
  Care / Emerging Treatments in Development / Key Companies & Institutions).
  Chosen for demo reliability (no live-internet dependency during the
  interview) and fidelity to towncrier's actual "answer from reviewed
  corpus" model.
- **Conditions to curate:** Type 2 Diabetes, Heart Failure, Rheumatoid
  Arthritis, Multiple Myeloma — chosen for a mix of chronic-disease scale,
  cardiology, autoimmune, and oncology-with-active-pipeline, to show range
  across guideline maturity and trial density.
- **Runtime: standalone Streamlit app**, not a Claude-Code-chat demo — looks
  like a real product to a stakeholder panel rather than a terminal
  conversation. Confirmed Jeremy has an Anthropic API key to wire in.
- **Synthesis approach:** app calls the Claude API per query, given *only*
  that condition's corpus file as context, instructed to synthesize the
  3-section structured briefing with inline citations. Cite-or-decline is
  enforced in the prompt: unsupported claims or unloaded conditions get an
  explicit decline, not a fabricated answer — this is itself a planned live-
  demo moment (query an unloaded condition, show it refuse gracefully).
- **Location:** new sibling folder `DEV/apps/jeremy/care-briefing` (this
  directory) — kept separate from `towncrier` itself since this is a
  derivative one-off for the challenge, not a change to the civic-records
  project.

### Corpus/app contract (drafted this session, not yet sent to curation session)
Corpus curation has been **handed off to a separate session** (likely
`dev-a8`, started ~16 min before this note, status busy — unconfirmed with
Jeremy which session it actually is). This session (jeremyodell-07) is now
focused on the app side and needs to hand `dev-a8` the schema below so both
halves snap together.

One markdown file per condition, e.g. `corpus/type-2-diabetes.md`:
```
---
condition: Type 2 Diabetes
aliases: ["T2D", "type II diabetes", "adult-onset diabetes"]
last_reviewed: 2026-08-28
sources:
  - id: ada-2026
    title: "ADA Standards of Care in Diabetes—2026"
    url: "https://..."
---
## Standard of Care
- Claim text. [ada-2026]
## Emerging Treatments in Development
- Claim text. [source-id]
## Key Companies & Institutions
- Claim text. [source-id]
```
Every bullet ends in a `[source-id]` tag resolving to frontmatter `sources`
— this is what makes cite-or-decline mechanically enforceable, not just
prompted.

### App architecture (drafted, pending final confirmation)
Streamlit `app.py`: load all `corpus/*.md` at startup (condition + aliases
drive the dropdown, no hardcoding) → on selection, call Claude with a
system prompt restricted to that file's text + a structured-output schema
(3 sections × bullets × citation ids) → render as 3 cards + a References
footer. Free-text box for an unloaded condition skips the LLM and renders
a decline card directly (the live guardrail/trust demo moment). Fallback:
if the API call fails, render the raw corpus section text so a network
hiccup never blanks the screen mid-demo.

### Immediate Next Steps (in order)
1. Confirm with Jeremy which session is doing corpus curation, then send it
   the schema above so output lands in the right shape.
2. Resolve the one open scope call below, then start `app.py`.
3. Build the Streamlit app per the architecture above — ~40 min budget.
4. End-to-end test, including the deliberate out-of-corpus decline case.
5. One pitch slide.
6. Stretch goal if time remains: "Publish" button exporting the on-screen
   briefing as a self-contained HTML file via the adapted Publisher role.

### DIRECTION CHANGE (supersedes the pre-loaded-corpus plan above)
Jeremy pivoted: **no longer 3-4 pre-loaded conditions** — wants **live
search** so the app works for any condition typed in, with real-time
results. He referenced "the stack we found in the other thread" as what
the live-search call should use, but did not name it or the thread yet.
**Corpus curation handed off to `dev-a8` was for the old pre-loaded-corpus
plan — needs to be reconciled/stopped once the live-search stack is
confirmed**, or repurposed as a fallback/seed cache rather than the
primary path.

### Open Questions (blocking) — RESOLVED by dev-a8, see update below
- **What is "the stack we found in the other thread"?** Asked Jeremy which
  session it's in and what it is (search API like Tavily/Exa/Perplexity,
  a library, something else) — awaiting his reply. This determines the
  whole live-search architecture, so nothing else proceeds until answered.
- Once known: does `dev-a8`'s corpus work still get used (e.g. as a cached
  fallback for the 4 original conditions) or is it fully superseded by
  live search?

---

## Update from jeremyodell-07 — resolved, building app.py now

Adopted dev-a8's `sources.py` as the primary data path; corpus/*.md is
fallback-only, rendered as-is (whichever frontmatter shape is actually on
disk) — no schema harmonization work, confirmed with dev-a8 via
cross-session message. `gather_briefing(query, wall_clock_budget_s=15.0) ->
BriefingBundle` is the integration point for `app.py`.

**Section mapping** (their 4 sources -> our 3 UI sections):
- Standard of Care <- `standard_of_care` (PubMed) + `approvals` (openFDA —
  what's actually approved now)
- Emerging Treatments in Development <- `emerging_treatments.trials`
  (ClinicalTrials.gov)
- Key Companies & Institutions <- `emerging_treatments.sponsors` +
  `approvals` manufacturers + `institutions` (NIH RePORTER)

**Orchestration for app.py:** call `gather_briefing()` (already handles
concurrency/timeouts/error-typing internally — no need to re-build a
ThreadPoolExecutor fan-out at this layer). Then one Claude synthesis call
takes the full bundle + its `.manifest()` status, and produces the 3-section
structured briefing (JSON/tool-use schema for reliable rendering) — cite-
or-decline per section enforced by requiring every bullet to tie to a
concrete item in the provided data, and by instructing the model to say
"unavailable" for a section whose underlying source(s) came back
not-`usable` rather than inventing content. Single synthesis call chosen
over 3 parallel per-section calls — lower risk/complexity given the clock,
and the real "parallel work" story is already told by `gather_briefing()`
hitting 4 live gov APIs concurrently.

**Built and running.** `app.py` is complete: corpus fallback loader (fuzzy-
matches `corpus/*.md` by condition name via `difflib`), `gather_briefing()`
call, single synthesis call forcing a tool-use schema (`emit_briefing`:
condition + exactly 3 sections, each with title/status/note/bullets, every
bullet requiring text+citation_label+citation_url), Streamlit UI (manifest
status badges, 3 section cards, corpus-fallback banner). `requirements.txt`
added. API key resolution checks `ANTHROPIC_API_KEY` then falls back to
`ORRERY_TENANT_DEFAULT_ANTHROPIC_API_KEY` / `ORRERY_TENANT_TOWNCRIER_ANTHROPIC_API_KEY`
(already present in the shell env) — deliberately not written to a file.

**Schema validation added per Jeremy's explicit request:** `tool_choice`
forcing makes the model *likely* to return conforming JSON but doesn't
guarantee it, so `validate_briefing()` now runs `jsonschema.validate()`
against the same schema server-side, plus checks JSON Schema can't express
alone (exact 3-section set present, every non-"ok" status carries a note,
every citation_url actually looks like a URL) — before anything reaches
`render_briefing()`. A validation failure is a distinct `SynthesisValidationError`,
caught separately from a raw API failure, both falling back to the corpus
file if there's a match.

Verified: `sources.py` smoke-tested live (`python sources.py "rheumatoid
arthritis"` — real PubMed/ClinicalTrials.gov/openFDA/NIH RePORTER data back).
App syntax-checked. Streamlit server running at http://localhost:8501
(background task, launched via `python -m streamlit run app.py` — the
`streamlit` console script isn't on PATH, module form is). Not yet
click-tested end-to-end in the browser with a real query typed in.

### Automated tests added (Jeremy asked for these before touching the UI)
`tests/test_sources.py` (16 tests) and `tests/test_app.py` (14 tests), all
mocked — no real network calls, run in ~3.5s. `requirements-dev.txt` added
(pytest + pytest-mock). Run with:
```
python -m pytest tests/ -v
```
Coverage: every `_request_json` failure classification (429/5xx retry,
4xx no-retry, timeout, connection error, unparseable JSON) never raises;
`SourceResult.usable` truth table; `BriefingBundle.manifest()` status
strings; the openFDA null-manufacturer/no-name record handling dev-a8
flagged; `gather_briefing()`'s wall-clock budget actually caps a hanging
source instead of blocking (this one caught a real bug — see below).
On the app side: `validate_briefing()` — valid case passes; every failure
mode Jeremy asked about is exercised (missing citation_url, bad status
enum, wrong section-title set, non-"ok" status without a note, a citation
that isn't a URL, wrong section count) and each raises the right exception
type; `bundle_all_unusable`; corpus load/match/frontmatter-strip.

**Bug the test suite caught (not a design flaw, a test-authoring bug):**
first draft of the wall-clock-budget test used `time.sleep(5)` to simulate
a hung source, but the suite's autouse "don't actually sleep during tests"
fixture monkeypatches `sources.time.sleep` — and `sources.time` is the
*same cached module object* as the test file's own `time` import, so the
simulated hang silently no-op'd and the test passed for the wrong reason
(instant completion, not budget-capping). Fixed by simulating the hang
with `threading.Event().wait()` instead, which isn't touched by that
monkeypatch. All 30 tests pass for real now.

**All 30 tests currently pass.** Not yet run: an actual end-to-end click
in the browser UI with a live query (the app has never been driven through
Streamlit itself, only unit-tested and smoke-tested at the module level).

### Immediate Next Steps
1. Run `python -m pytest tests/ -v` yourself to confirm green before using
   the UI (Jeremy's ask this turn).
2. First real browser click-test: type a condition, confirm manifest
   badges + 3 rendered cards + citations look right end-to-end.
3. Test the deliberate failure/decline path in the browser (an obscure
   condition, or temporarily break a source) to confirm graceful
   degradation shows up correctly in the UI, not just in code/tests.
4. One pitch slide.
5. Stretch goal if time remains: "Publish" button exporting the on-screen
   briefing as a self-contained HTML file (adapted Publisher role).

### Grounding gate added (Jeremy: "user MUST know data is not made up")
Found and closed a real gap: `validate_briefing()` only checked a citation
*looked like* a URL (`startswith http`), which does not stop a model from
inventing a plausible-but-fake `pubmed.ncbi.nlm.nih.gov/9999999/`. Fixed
with a second, independent gate:
- `build_citation_index(bundle)` builds the exact, real set of
  label->url pairs traceable to the raw API data (per-item for
  PubMed/ClinicalTrials.gov, which carry real URLs; one honest
  source-level citation for openFDA/NIH RePORTER, which carry NO
  per-record URL in their API responses — cites the real query
  endpoint/portal instead of fabricating a fake deep link).
- That index is the *only* thing the model is told it may cite from
  (SYSTEM_PROMPT rewritten accordingly), and is passed to `synthesize()`
  as `allowed_citations` in the context.
- `assert_citations_grounded()` then checks, post-response, that every
  returned (citation_label, citation_url) pair is a byte-for-byte match
  to one actually in that index — not "looks like a URL," an exact match
  to a citation we actually fetched. Runs after `validate_briefing()` in
  `synthesize()`; either failure is a `SynthesisValidationError`, same
  fallback path as before.
- UI: `render_briefing()` now shows a "grounded, not from general
  knowledge" trust line and a References expander listing every citation
  actually used, deduplicated.
6 new tests added (citation index construction, source-omission when a
source is unusable, a grounded citation passing, a fabricated-but-real-
looking URL rejected, a real URL with a mismatched label rejected).
**All 36 tests pass** (`python -m pytest tests/ -v`).

### Chat front end built + FIRST LIVE END-TO-END PASS CONFIRMED
Jeremy asked for a simple, intuitive chat UI for medical administrators:
ask for a condition, see progress, get results. Rewrote the presentation
layer of `app.py` only — pipeline functions untouched, all 36 tests still
green:
- `st.chat_input` chat loop, history in `st.session_state["messages"]`
  (assistant turns stored as plain result dicts so replay on rerun never
  re-queries sources or the model).
- `run_pipeline()` runs one turn inside a live `st.status` panel showing
  each stage: parallel source queries -> per-source health lines
  (green/white/red) -> synthesizing -> "Verified N citations, none
  invented" -> collapses to "Briefing ready". Every failure path returns
  a result dict (corpus fallback / honest error), never raises.
- Sidebar: description, 5 example-condition buttons (set a pending query),
  disclaimer, sources line, Clear conversation.

**Verified live in Chrome (not just unit tests):** typed "psoriatic
arthritis" -> all 5 sources ok in 4.0s -> synthesis -> citation
verification passed -> 3 sections rendered, 11 grounded references
(real PMIDs, NCT IDs incl. NCT04908202 deucravacitinib/BMS, NCT07295509
picankibart/Innovent; Johns Hopkins/Northwestern/UPenn via RePORTER).
Streamlit server still running in background on :8501
(`python -m streamlit run app.py`).

Content observation for the demo, not a bug: openFDA's indication search
surfaced "Prednisone is labeled for psoriatic arthritis" as a Standard of
Care bullet — technically grounded (real label), but a weak SoC claim.
Could tighten the prompt to prefer guideline/biologic evidence over
generic-label matches if time allows; otherwise fine to acknowledge live.

### BUILT + VERIFIED LIVE: three Researcher agents (Jeremy: "fastest and
### easiest route for the demo, improve later" -> fetch-once approved)
- `grounding.py` (new): shared trust layer — SECTION_SCHEMA/BRIEFING_SCHEMA,
  `build_citation_index(bundle, fields)` (per-stack scoping), `slice_bundle`,
  `validate_section`, `assert_section_grounded`, `SynthesisValidationError`.
- `agents.py` (new): `RESEARCHERS` registry (soc / pipeline / players, each
  with title, bundle fields = its stack, stack_label, role focus),
  `run_researcher()` (own citation index -> own Claude call with
  single-section `emit_section` tool -> validate -> ground; returns
  (section, error) — never raises, declines honestly; skips the model call
  entirely if its stack has no usable data), `run_all_researchers()`
  (ThreadPoolExecutor, yields as each finishes), `assemble_briefing()`.
- `app.py` rewritten: one `gather_briefing()` fetch, then progress panel
  shows "Dispatching 3 Researcher agents" with each stack, then a per-agent
  ✅/🚫 line as each returns ("N claims, N citations verified"), label
  "Briefing ready — 3/3 sections verified". Old 3-section `synthesize()`
  removed; `validate_briefing`/`assert_citations_grounded` kept as thin
  wrappers over grounding.py so existing tests still hold.
- `tests/test_agents.py` (new, 9 tests): stack coverage, per-agent citation
  scoping (SoC can't cite CT.gov URL it wasn't given — real URL, wrong
  stack -> rejected), wrong-title rejection, API failure -> honest decline,
  no-usable-sources skips model call, assembly ordering/gap-filling,
  run_all yields all 3 even when one fails. **45 tests green.**
- **Live in Chrome, "heart failure":** 5/5 sources 5.1s -> 3 agents
  dispatched -> 3/3 verified, 15 references. SoC agent self-reported
  "partial" with a candid note that no ACC/AHA/ESC guideline was in the
  PubMed slice (only 2026 articles + Entresto label) — the "prefer
  guideline evidence" focus working as intended; good honesty demo moment.
- dev-a8 independently ran the full chain live on a misspelled query
  ("prostrate cancer"): PubMed/CT.gov/RePORTER tolerated the typo, openFDA
  came back empty, SoC + Players correctly downgraded to "partial". Field
  mapping confirmed against real data shapes, not just code.

Improvement candidates (deferred per "improve later"): PubMed query could
add a guideline-specific filter/term so SoC gets ACC/AHA/ESC docs; RePORTER
bullets are source-level cites only (API gives no per-project URL).

### Original proposal (kept for reference): three Researcher agents
Jeremy wants the app tied into per-section "medical condition agents",
each with its own stack of sites, making three separate curation calls.
Design presented in chat, not yet built:
- `soc_researcher` — Standard of Care — PubMed (guideline/review) + openFDA
- `pipeline_researcher` — Emerging Treatments — ClinicalTrials.gov + PubMed
  (investigational reviews)
- `players_researcher` — Key Companies & Institutions — CT.gov sponsors +
  openFDA manufacturers + NIH RePORTER
Each agent: fetch its stack (reuse dev-a8 `fetch_*`) -> own citation index
-> own Claude call with a single-section schema -> own validate + grounding
gate -> verified Section or typed failure. Orchestrator fans 3 out
concurrently, progress panel shows each independently, assembles; one
agent failing -> that section "unavailable", others still render.
Files: new `agents.py`, `app.py` rewired, new tests. Ping dev-a8 with the
stack mapping once approved.

### Intake step added (Jeremy's first real test hit a gap)
Jeremy typed "i want a brief on prostate cancer" and got the "nothing
found" error. Root cause (confirmed with `python sources.py` both ways):
the raw sentence was sent verbatim as the search term to all 4 APIs ->
5/5 empty; bare "prostate cancer" -> 5/5 ok. Nothing was unhooked — the
chat UI invited natural language but the pipeline never extracted the
condition. Fix, mirroring towncrier's Intake role: `agents.extract_condition
(message, client) -> (condition, recognized)` — small Claude call with an
`extract_condition` tool (expands abbreviations, fixes misspellings,
`is_condition=False` for non-medical messages); falls back to the raw text
on any failure so it can never block a search. `run_pipeline` now starts
with "Reading your request…", shows "Understood: **prostate cancer**" when
the text changed, then researches the extracted condition. Non-condition
input gets a polite "tell me which condition" reply instead of a search.
3 new tests -> **48 green.** Verified live: the exact failing sentence now
proceeds to "Researching prostate cancer…".

**Second bug surfaced by the same test run:** Key Companies & Institutions
came back "The synthesis call for this section failed (ValidationError)".
Two causes: (1) `jsonschema.ValidationError` isn't a
`SynthesisValidationError`, so it fell into the generic API-failure bucket
with no reason shown; (2) the Players agent had `max_tokens=2048` and
prostate cancer returns many trials with very long CT.gov titles as
citation labels -> tool-call JSON truncated -> bullet missing a required
field. Fix in `run_researcher`: catch `jsonschema.ValidationError` and
re-raise as `SynthesisValidationError("schema: <message>")`; detect
`stop_reason == "max_tokens"` explicitly; `max_tokens` 2048 -> 4096; prompt
now caps sections at 8 one-sentence bullets. Re-run of the same sentence:
**3/3 sections verified, 12 references** (was 2/3). Server PID changed on
restart (task bkj14gth0).

### Section summaries added (Jeremy: "user needs to read more into it")
Each section now leads with a plain-language **summary** paragraph (2-4
sentences for a strategy leader), then an "Evidence & sources" header, then
the cited bullets with links. `grounding.SECTION_SCHEMA` gained an optional
`summary` string; the Researcher prompt ALWAYS requires one and constrains
it to restate only what the cited bullets establish (no new facts);
`app.render_briefing` renders summary -> evidence header -> bullets.
`summary` is optional in the schema (not validated for presence) so
existing tests pass unchanged — grounding of the summary text rests on the
prompt constraint, not a mechanical check; the bullets beneath remain the
mechanically verified layer. 48 tests green. Verified live ("rheumatoid
arthritis"): 3/3 sections, 15 refs, each section shows summary -> evidence.
Minor polish candidate: on partial sections the long status `note` caption
sits above the summary and reads a bit doubled — could shorten the note
or move it below the summary.

**Ops gotcha hit:** Streamlit hot-reloads `app.py` but NOT imported modules
(`agents.py`, `grounding.py`, `sources.py`) — after editing those, the
running server threw ImportError until restarted. Restart with: kill the
PID on :8501, then `python -m streamlit run app.py --server.headless true
--server.port 8501`. Server PID changes on every restart — check `netstat -ano | grep :8501`.

### Open Questions
- None blocking. (Fetch-once vs per-agent: RESOLVED, fetch-once.)

### Immediate Next Steps
1. Test the failure/decline path in the browser (garbage condition).
2. One pitch slide.
3. Stretch: "Publish" button exporting the briefing as self-contained HTML.

---

## Update from dev-a8 (this session) — live-search stack built + a real schema conflict

Saw the DIRECTION CHANGE note above after already building the live-search
layer in this session (Jeremy gave the same "live search, error handling"
direction here independently). Answering your blocking question directly:
**"the stack" = free, keyless public APIs** — ClinicalTrials.gov API v2,
PubMed E-utilities, openFDA, NIH RePORTER, NLM Clinical Table Search
Service — researched two sessions ago in `dev`'s own `progress.md` (see
that file's Session 08-28 entry) and now implemented in
`care-briefing/sources.py` in this project. Not Tavily/Exa/Perplexity —
plain REST calls to government/NLM endpoints, no search-API subscription
needed.

`sources.py` is done and smoke-tested live (see the Pivot section above
this one for the full build log and bugs it caught). It exposes
`gather_briefing(query, wall_clock_budget_s=15.0) -> BriefingBundle` with
per-source `SourceResult`s and a `.manifest()` status summary — this is
what your Streamlit `app.py` should call instead of loading `corpus/*.md`.

**Real conflict to reconcile, not just an FYI:** the corpus files I had 4
subagents curate (`corpus/*.md`, done in `dev`'s earlier turn, before
either session had the pivot) use `doc_type: condition-briefing`
frontmatter with inline markdown-link citations per claim
(`... claim text [source name](https://...)`). Your drafted contract above
uses a different shape — `sources:` id list in frontmatter + `[source-id]`
tags per bullet. **Both exist on disk now and don't match each other.**
Given the pivot, I'd treat the 4 corpus files as the offline-fallback path
(per your architecture note: "if the API call fails, render the raw corpus
section text") rather than the primary path, in which case the schema
mismatch may not matter much — but if you want the fallback path to
actually render, `app.py`'s corpus loader needs to target whichever
frontmatter shape actually exists on disk (`doc_type`/inline-links), not
the `sources:`/`[source-id]` contract drafted above, since that's not what
got built. Flagging rather than silently picking one — this is a two-
session judgment call, not mine alone to resolve.

---

## Update from dev-a8 — investigated a live "ValidationError" on prostate cancer

Jeremy hit `🚫 Key Companies & Institutions — The synthesis call failed
(ValidationError)` on a live query and asked why. Findings:

- **Root cause of the vague message:** `grounding.validate_section()` let a
  raw `jsonschema.exceptions.ValidationError` escape uncaught, so the only
  handler that caught it (the generic except in `agents.run_researcher`)
  reported just the exception's class name and discarded `exc.message` —
  the one thing that would say what the model actually got wrong. Fixed:
  `validate_section()` now catches it itself and re-raises as
  `SynthesisValidationError(f"schema violation: {exc.message}")`.
- Whoever's driving `agents.py` now (session identity changed mid-turn,
  `jeremyodell-07` -> `jeremyodell-cb`/`jeremyodell-f6`, unclear which)
  independently added the equivalent wrap at the `run_researcher` call site
  around the same time, plus `max_tokens` truncation handling. Both fixes
  coexist fine — the `agents.py` one is now dead code since
  `validate_section` never lets the raw exception through anymore, but
  it's harmless. 48/48 tests pass with both in place.
- **Could not reproduce the actual failure**: 7 live API calls for that
  exact section/condition (1 + 6 more), all validated clean. Looks like a
  rare nondeterministic model-output glitch, not a systemic bug — unlike
  the earlier PubMed MeSH-tag issue, which was 100% reproducible.
- **Follow-up: Jeremy asked directly whether retry handling existed — it
  didn't, for this specific failure class, so implemented it.** Added
  `MAX_SYNTHESIS_ATTEMPTS = 2` to `agents.py`: `run_researcher` now retries
  once on `SynthesisValidationError` (bad structured output on an
  otherwise-successful call) before giving up — generic transport failures
  are untouched since the Anthropic SDK already retries those itself
  (`max_retries=2` default: connection errors, 408/409/429/5xx) before this
  code ever sees them, so this closes a gap the SDK's own retries don't
  cover, not a duplicate of it. If both attempts fail, the section still
  degrades to `unavailable` with an honest note (now naming both the retry
  count and the last real error) — never silently retries forever.
  Verified, not just written: monkeypatched `validate_section` to force
  attempt 1 to fail, ran `run_researcher` against a real live API call —
  confirmed exactly 2 calls made, attempt 2 recovered cleanly (`status:
  ok`, 8 bullets, no error surfaced). 48/48 tests still pass.

### Committed to GitHub (Jeremy's request)
Repo initialized and pushed: **https://github.com/jeremyodell/care-briefing**
(private, `master`, initial commit `fed083f`, account `jeremyodell` /
jeremyodell@gmail.com — the github.com login in `gh auth status`, not the
Denali GHE account). `.gitignore` excludes `__pycache__/`, `.pytest_cache/`,
`*.log`, `.env`, `.venv/`. Secret scan before commit: no `sk-ant-` strings
in any tracked file — the app reads keys from env vars only. Commit from
here on with the same identity; this progress.md itself is tracked.
