---
from_session: dev-a8
date: 2026-08-28
subject: Care Briefing — live data-source layer (verified, cited)
---

# Care Briefing — data-layer status

Everything below is either (a) a file on disk you can open at the given
path/line, or (b) a live API response this session actually received and
is quoting — nothing here is asserted from memory. Where a claim isn't
independently checkable, it's flagged as such rather than stated flat.

## What was built

**`care-briefing/sources.py`** — the live data-fetch layer for the pivot
from a 4-condition pre-loaded corpus to arbitrary-condition live search
(Jeremy's direction, this session). Entry point:

```
gather_briefing(query: str, wall_clock_budget_s: float = 15.0) -> BriefingBundle
```

Fans out to four public APIs concurrently, each wrapped so failures come
back as data (a `SourceResult`), never an exception:

| Section | Source | Endpoint | Auth |
|---|---|---|---|
| Standard of care | PubMed E-utilities | `eutils.ncbi.nlm.nih.gov/entrez/eutils/{esearch,esummary}.fcgi` | none (optional `NCBI_API_KEY`) |
| Emerging treatments + sponsors | ClinicalTrials.gov API v2 | `clinicaltrials.gov/api/v2/studies` | none |
| Recent approvals | openFDA | `api.fda.gov/drug/label.json` | none |
| Institutions | NIH RePORTER | `api.reporter.nih.gov/v2/projects/search` | none |
| Condition normalization (display/ICD-10 only, not a search substitute) | NLM Clinical Table Search Service | `clinicaltables.nlm.nih.gov/api/conditions/v3/search` | none |

Source: `care-briefing/sources.py:1-50` (module docstring + imports) and
per-function definitions further down the same file.

## Verification — this was smoke-tested live, not just designed

Ran `python sources.py "<condition>"` against real conditions this
session and read the actual HTTP responses. Confirmed runs:

- `"psoriatic arthritis"` — all 5 sources returned `ok`; PubMed returned 3
  real articles including PMID `42661716` (Frontiers in Immunology, IL-17
  inhibitor network meta-analysis), ClinicalTrials.gov returned real NCT
  IDs (e.g. `NCT05772325`, lead sponsor Diakonhjemmet Hospital), NIH
  RePORTER returned funded projects at Johns Hopkins and UCSD.
- `"multiple myeloma"` — all 5 sources `ok`, 2.43s total wall-clock.
- `"zzznonexistent-condition-xyz123"` (garbage input) — degraded to
  `empty` on 4 of 5 sources without crashing, confirming the no-match
  path works, not just the happy path.
- Tight-budget test (`wall_clock_budget_s=0.05`) — confirmed the
  orchestrator returns a fully-populated `timeout` status on every source
  within ~0.7s instead of hanging or raising.

## Bugs this caught before they could hit the live demo

Each of these was a real, reproduced failure, not a hypothetical:

1. NLM Clinical Table Search Service silently omits synonyms/ICD-10
   unless `df=` is passed explicitly — confirmed via direct `curl`
   against `clinicaltables.nlm.nih.gov`, comparing the response with and
   without the param.
2. The service's `primary_name` field ("Arthritis - psoriatic") is a
   consumer-display string, not a real MeSH heading — feeding it into
   PubMed's `[MeSH Terms]` filter silently zeroed a query that the raw
   search term found fine. Reproduced by diffing PubMed output before/
   after the substitution.
3. `ThreadPoolExecutor` used as a context manager blocks on `__exit__`
   until every worker thread finishes, regardless of any timeout
   parameter passed elsewhere — reproduced with the 0.05s-budget test
   above, which hung until fixed.
4. A tight timeout raised `TimeoutError` out of `as_completed()`
   uncaught, crashing the whole orchestrator — reproduced with the same
   test, now caught and backfilled per-source.
5. openFDA label search returns records with no `openfda` cross-reference
   block (compounded/private-label generics) — was rendering as
   `null`/`null` noise in the multiple-myeloma/psoriatic-arthritis runs;
   now filtered.

Full narrative and code diffs: `care-briefing/progress.md`, section
"Update from dev-a8" (this session's entries).

## Fallback path (not primary — flagging status honestly)

`care-briefing/corpus/*.md` — 4 condition files (Type 2 Diabetes, Heart
Failure, Rheumatoid Arthritis, Multiple Myeloma) curated earlier this
session by 4 parallel subagents, each claim cited inline to a real URL
the curating agent fetched or found via search. These are **not the
primary data path anymore** — confirmed with `jeremyodell-07` (the
session building `app.py`) via cross-session message that they're
fallback-only, rendered as-is if live sources are unavailable. Two
citation caveats on that corpus, reported by the curating agents
themselves, not discovered after the fact:
- `type-2-diabetes.md`: 2 citations rest on secondary sources (HCPLive,
  Pharmacy Times) because the primary ADA journal pages 403'd on fetch —
  facts cross-checked as consistent, but not independently verified via
  primary source.
- `multiple-myeloma.md`: FDA.gov citations rest on WebSearch confirming
  the page is real/correctly-titled, since fda.gov blocked direct fetch —
  backed by a second fetchable source where possible.

## Open items (not resolved, not glossed over)

- No `NCBI_API_KEY` set — fine at current query volume (3 req/s ceiling),
  worth adding (`NCBI_API_KEY` env var, already wired in `sources.py`) if
  the demo gets concurrent traffic.
- `app.py` (Streamlit UI + Claude synthesis over `BriefingBundle`) is
  being built by `jeremyodell-07`, not yet complete as of this update —
  status there should be confirmed with that session directly, not
  assumed from here.
