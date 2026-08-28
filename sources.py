"""
Live data-source layer for Care Briefing.

Pivot from the pre-loaded-corpus design: the app now takes an arbitrary
condition query from the end user at runtime and fans it out to public
health-data APIs, instead of reading a pre-curated markdown file. That
means network failure is now part of the live demo, not something curated
away in advance — this module exists to make that failure survivable and
honest rather than something that crashes the UI or quietly fabricates.

Design rules:
- No fetch_* function ever raises to its caller. Every call returns a
  SourceResult so failures are data, not exceptions the UI has to catch.
- Every failure is classified (timeout / rate_limited / http_error /
  network_error / no_match) so the synthesis prompt and the UI can say
  something honest and specific instead of a generic "something went
  wrong" — this is the live-query equivalent of the corpus design's
  cite-or-decline rule: a source that is down is not the same as a
  source that checked and found nothing, and the briefing should say
  which one happened.
- Transient failures (timeout, connection error, 5xx, 429) get one retry
  with backoff; a source-side 4xx (bad query) does not retry — retrying
  a malformed query just wastes the demo's time budget.
- gather_briefing() runs every source concurrently with a wall-clock
  budget, so one slow/hung source can't stall the whole briefing — it
  just gets marked as unavailable and the rest of the briefing still
  renders.
"""

from __future__ import annotations

import os
import random
import time
import logging
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

log = logging.getLogger("care_briefing.sources")

USER_AGENT = "care-briefing-prototype/0.1 (contact: jeremyodell@gmail.com)"
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")  # optional: 3 req/s -> 10 req/s

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class SourceResult:
    source: str
    ok: bool
    data: Any = None
    empty: bool = False           # ok=True but the query legitimately found nothing
    error_type: str | None = None  # "timeout" | "rate_limited" | "http_error" | "network_error" | "no_match"
    error: str | None = None       # human-readable, safe to show in the UI
    elapsed_s: float = 0.0

    @property
    def usable(self) -> bool:
        """True if there's real data to hand to the synthesis step."""
        return self.ok and not self.empty


@dataclass
class BriefingBundle:
    query: str
    normalized: SourceResult
    standard_of_care: SourceResult
    emerging_treatments: SourceResult
    approvals: SourceResult
    institutions: SourceResult
    total_elapsed_s: float = 0.0

    def manifest(self) -> dict:
        """Per-source status summary for the UI and the synthesis prompt —
        this is what lets the app say 'ClinicalTrials.gov unavailable,
        answer below is guideline-only' instead of silently degrading."""
        def status(r: SourceResult) -> str:
            if r.usable:
                return "ok"
            if r.ok and r.empty:
                return "empty"
            return f"unavailable ({r.error_type})"

        return {
            "condition normalization": status(self.normalized),
            "standard of care (PubMed)": status(self.standard_of_care),
            "emerging treatments (ClinicalTrials.gov)": status(self.emerging_treatments),
            "recent approvals (openFDA)": status(self.approvals),
            "institutions (NIH RePORTER)": status(self.institutions),
        }


# --------------------------------------------------------------------------
# Core request helper: timeout + classified retry, never raises
# --------------------------------------------------------------------------

def _request_json(
    source: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: tuple[float, float] = (3.05, 7.0),  # (connect, read)
    max_retries: int = 1,
) -> SourceResult:
    start = time.monotonic()
    attempt = 0
    last_error_type = "network_error"
    last_error = "unknown error"

    while attempt <= max_retries:
        try:
            resp = _session.request(
                method, url, params=params, json=json_body, timeout=timeout
            )
        except requests.exceptions.Timeout:
            last_error_type, last_error = "timeout", f"{source} did not respond in time"
        except requests.exceptions.ConnectionError as e:
            last_error_type, last_error = "network_error", f"couldn't reach {source}: {e.__class__.__name__}"
        except requests.exceptions.RequestException as e:
            last_error_type, last_error = "network_error", f"{source} request failed: {e.__class__.__name__}"
        else:
            if resp.status_code == 429:
                last_error_type, last_error = "rate_limited", f"{source} rate-limited this request"
                retry_after = resp.headers.get("Retry-After")
                _backoff(attempt, floor=float(retry_after) if retry_after else None)
                attempt += 1
                continue
            if 500 <= resp.status_code < 600:
                last_error_type, last_error = "http_error", f"{source} returned {resp.status_code}"
                _backoff(attempt)
                attempt += 1
                continue
            if 400 <= resp.status_code < 500:
                # Bad query on our side (or condition genuinely not found) —
                # retrying won't help, don't burn the time budget.
                return SourceResult(
                    source=source, ok=False, error_type="http_error",
                    error=f"{source} rejected the query ({resp.status_code})",
                    elapsed_s=time.monotonic() - start,
                )
            try:
                return SourceResult(
                    source=source, ok=True, data=resp.json(),
                    elapsed_s=time.monotonic() - start,
                )
            except ValueError:
                return SourceResult(
                    source=source, ok=False, error_type="http_error",
                    error=f"{source} returned an unparseable response",
                    elapsed_s=time.monotonic() - start,
                )
        attempt += 1
        if attempt <= max_retries:
            _backoff(attempt)

    return SourceResult(
        source=source, ok=False, error_type=last_error_type, error=last_error,
        elapsed_s=time.monotonic() - start,
    )


def _backoff(attempt: int, floor: float | None = None) -> None:
    delay = floor if floor is not None else min(1.5 * (2 ** attempt), 5.0)
    time.sleep(delay + random.uniform(0, 0.25))


# --------------------------------------------------------------------------
# Condition normalization — NLM Clinical Table Search Service
# --------------------------------------------------------------------------

def normalize_condition(query: str) -> SourceResult:
    """Free-text condition -> ICD-10-CM code(s) + synonym terms, so the
    other sources (which expect different vocabularies) get a consistent
    set of search terms. Falls back to the raw query string on failure —
    callers should check .ok to know whether to trust the normalization
    or treat the terms as a best-effort guess."""
    r = _request_json(
        "NLM Clinical Table Search Service", "GET",
        "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search",
        # df must be requested explicitly — without it the API returns only
        # the primary name column, no synonyms/ICD-10 (found via smoke test:
        # indexing into the missing column raised, misreported as http_error).
        params={"terms": query, "maxList": 5, "df": "primary_name,synonyms,icd10cm_codes"},
    )
    if not r.ok:
        return r
    try:
        payload = r.data
        count = payload[0]
        display_rows = payload[3] or []
        if count == 0 or not display_rows:
            return SourceResult(source=r.source, ok=True, empty=True, elapsed_s=r.elapsed_s)
        row = display_rows[0]
        primary_name = row[0]
        synonyms = row[1] if len(row) > 1 else ""
        icd10 = row[2] if len(row) > 2 else ""
        terms = [primary_name] + [s.strip() for s in (synonyms or "").split(";") if s.strip()]
        return SourceResult(
            source=r.source, ok=True,
            data={"matched_term": primary_name, "terms": terms[:6], "icd10cm": icd10 or None},
            elapsed_s=r.elapsed_s,
        )
    except (IndexError, TypeError):
        return SourceResult(
            source=r.source, ok=False, error_type="http_error",
            error="unexpected response shape from NLM condition lookup",
            elapsed_s=r.elapsed_s,
        )


# --------------------------------------------------------------------------
# Standard of care — PubMed E-utilities (guideline/review articles)
# --------------------------------------------------------------------------

def fetch_pubmed(condition: str, limit: int = 6) -> SourceResult:
    # Deliberately NOT tagging the condition with [MeSH Terms]: it requires
    # an exact match to the real MeSH heading text, and most colloquial
    # condition names aren't that text (e.g. "prostate cancer" isn't a
    # heading — "Prostatic Neoplasms" is; "type 2 diabetes" isn't either —
    # "Diabetes Mellitus, Type 2" is). Forcing the tag silently returned 0
    # results for any *cancer condition and for type 2 diabetes — found by
    # testing "prostate cancer" live, confirmed as systemic by testing 8
    # conditions directly against esearch. Untagged, PubMed's own automatic
    # term mapping resolves the colloquial name to the right MeSH heading
    # correctly (verified: 34,405 hits for prostate cancer this way, 0 the
    # other) — same lesson as the earlier normalization-substitution bug,
    # just found in a different call site.
    params = {
        "db": "pubmed",
        "term": f"{condition} AND (guideline[pt] OR review[pt])",
        "retmax": limit,
        "sort": "date",
        "retmode": "json",
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY

    search = _request_json(
        "PubMed", "GET",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params=params,
    )
    if not search.ok:
        return search

    ids = search.data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return SourceResult(source="PubMed", ok=True, empty=True, elapsed_s=search.elapsed_s)

    time.sleep(0.35 if not NCBI_API_KEY else 0.11)  # stay under the 3/10 req-s ceiling

    sum_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if NCBI_API_KEY:
        sum_params["api_key"] = NCBI_API_KEY
    summary = _request_json(
        "PubMed", "GET",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params=sum_params,
    )
    if not summary.ok:
        return summary

    result = summary.data.get("result", {})
    articles = []
    for pmid in result.get("uids", ids):
        item = result.get(pmid, {})
        if not item:
            continue
        articles.append({
            "pmid": pmid,
            "title": item.get("title"),
            "journal": item.get("fulljournalname") or item.get("source"),
            "pubdate": item.get("pubdate"),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    if not articles:
        return SourceResult(source="PubMed", ok=True, empty=True, elapsed_s=summary.elapsed_s)
    return SourceResult(source="PubMed", ok=True, data=articles, elapsed_s=summary.elapsed_s)


# --------------------------------------------------------------------------
# Emerging treatments + sponsors — ClinicalTrials.gov API v2
# --------------------------------------------------------------------------

def fetch_clinicaltrials(condition: str, limit: int = 10) -> SourceResult:
    r = _request_json(
        "ClinicalTrials.gov", "GET",
        "https://clinicaltrials.gov/api/v2/studies",
        params={
            "query.cond": condition,
            "filter.overallStatus": "RECRUITING,ACTIVE_NOT_RECRUITING",
            "pageSize": limit,
            "format": "json",
        },
    )
    if not r.ok:
        return r

    studies = r.data.get("studies", [])
    if not studies:
        return SourceResult(source="ClinicalTrials.gov", ok=True, empty=True, elapsed_s=r.elapsed_s)

    trials, sponsors = [], set()
    for s in studies:
        proto = s.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        design = proto.get("designModule", {})
        sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
        status_mod = proto.get("statusModule", {})

        lead = (sponsor_mod.get("leadSponsor") or {}).get("name")
        if lead:
            sponsors.add(lead)
        for c in sponsor_mod.get("collaborators", []) or []:
            if c.get("name"):
                sponsors.add(c["name"])

        nct_id = ident.get("nctId")
        trials.append({
            "nct_id": nct_id,
            "title": ident.get("briefTitle"),
            "phases": design.get("phases", []),
            "status": status_mod.get("overallStatus"),
            "lead_sponsor": lead,
            "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None,
        })

    return SourceResult(
        source="ClinicalTrials.gov", ok=True,
        data={"trials": trials, "sponsors": sorted(sponsors)},
        elapsed_s=r.elapsed_s,
    )


# --------------------------------------------------------------------------
# Recent approvals — openFDA drug label search
# --------------------------------------------------------------------------

def fetch_openfda_approvals(condition: str, limit: int = 5) -> SourceResult:
    r = _request_json(
        "openFDA", "GET",
        "https://api.fda.gov/drug/label.json",
        params={"search": f'indications_and_usage:"{condition}"', "limit": limit},
    )
    if not r.ok:
        # openFDA returns 404 for zero-match queries, not an empty list —
        # treat a 404 on this endpoint as "empty", not "unavailable".
        if r.error_type == "http_error" and "404" in (r.error or ""):
            return SourceResult(source="openFDA", ok=True, empty=True, elapsed_s=r.elapsed_s)
        return r

    results = r.data.get("results", [])
    if not results:
        return SourceResult(source="openFDA", ok=True, empty=True, elapsed_s=r.elapsed_s)

    labels = []
    for item in results:
        openfda = item.get("openfda", {})
        generic = (openfda.get("generic_name") or [None])[0]
        brand = (openfda.get("brand_name") or [None])[0]
        if not (generic or brand):
            # Some label records (compounded/private-label generics) don't
            # carry the openfda cross-reference block at all — not a parse
            # bug, just an unnamed record. Drop rather than show nulls.
            continue
        labels.append({
            "brand_name": brand,
            "generic_name": generic,
            "manufacturer": (openfda.get("manufacturer_name") or [None])[0],
        })
    if not labels:
        return SourceResult(source="openFDA", ok=True, empty=True, elapsed_s=r.elapsed_s)
    return SourceResult(source="openFDA", ok=True, data=labels, elapsed_s=r.elapsed_s)


# --------------------------------------------------------------------------
# Institutions — NIH RePORTER
# --------------------------------------------------------------------------

def fetch_reporter(condition: str, limit: int = 8) -> SourceResult:
    r = _request_json(
        "NIH RePORTER", "POST",
        "https://api.reporter.nih.gov/v2/projects/search",
        json_body={
            "criteria": {
                "advanced_text_search": {
                    "operator": "and",
                    "search_field": "projecttitle,terms,abstracttext",
                    "search_text": condition,
                },
                "include_active_projects": True,
            },
            "limit": limit,
            "sort_field": "project_start_date",
            "sort_order": "desc",
        },
    )
    if not r.ok:
        return r

    results = r.data.get("results", [])
    if not results:
        return SourceResult(source="NIH RePORTER", ok=True, empty=True, elapsed_s=r.elapsed_s)

    projects = []
    institutions = set()
    for p in results:
        org = (p.get("organization") or {}).get("org_name")
        if org:
            institutions.add(org)
        projects.append({
            "title": p.get("project_title"),
            "org": org,
            "fiscal_year": p.get("fiscal_year"),
            "core_project_num": p.get("core_project_num"),
        })
    return SourceResult(
        source="NIH RePORTER", ok=True,
        data={"projects": projects, "institutions": sorted(institutions)},
        elapsed_s=r.elapsed_s,
    )


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def gather_briefing(query: str, wall_clock_budget_s: float = 15.0) -> BriefingBundle:
    """Runs every source concurrently, bounded by a total wall-clock budget
    so the UI never hangs on one slow API. Sources that don't finish in time
    come back as a 'timeout' SourceResult, not an exception."""
    start = time.monotonic()

    norm = normalize_condition(query)
    # Deliberately NOT substituting the normalized term into the search
    # calls below: found via smoke test that CTSS's primary_name is a
    # consumer-display string ("Arthritis - psoriatic"), not a real MeSH
    # heading, and feeding it into PubMed's "[MeSH Terms]" filter silently
    # zeroed out results that the raw query found fine. PubMed's own
    # automatic term mapping (and ClinicalTrials.gov's/openFDA's free-text
    # search) already handle natural-language condition names reasonably —
    # normalization is used for display/ICD-10 metadata only, not as a
    # search-term replacement.
    search_term = query

    jobs: dict[str, Callable[[], SourceResult]] = {
        "standard_of_care": lambda: fetch_pubmed(search_term),
        "emerging_treatments": lambda: fetch_clinicaltrials(search_term),
        "approvals": lambda: fetch_openfda_approvals(search_term),
        "institutions": lambda: fetch_reporter(search_term),
    }

    results: dict[str, SourceResult] = {}
    # Not using `with ThreadPoolExecutor(...) as pool:` on purpose — its
    # __exit__ calls shutdown(wait=True), which blocks until every worker
    # thread finishes regardless of our budget, defeating the whole point
    # of wall_clock_budget_s (found via smoke test with a near-zero
    # budget). shutdown(wait=False, cancel_futures=True) below lets us
    # return on time and abandon whatever hasn't finished.
    pool = ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        try:
            for future in as_completed(futures, timeout=wall_clock_budget_s):
                results[futures[future]] = future.result()
        except TimeoutError:
            pass  # whatever's still pending gets filled in as a timeout below
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for name in jobs:
        results.setdefault(name, SourceResult(
            source=name, ok=False, error_type="timeout",
            error=f"{name} did not complete within the {wall_clock_budget_s:.0f}s budget",
        ))

    return BriefingBundle(
        query=query,
        normalized=norm,
        standard_of_care=results["standard_of_care"],
        emerging_treatments=results["emerging_treatments"],
        approvals=results["approvals"],
        institutions=results["institutions"],
        total_elapsed_s=time.monotonic() - start,
    )


if __name__ == "__main__":
    import json as _json
    import sys

    logging.basicConfig(level=logging.INFO)
    condition = sys.argv[1] if len(sys.argv) > 1 else "psoriatic arthritis"
    bundle = gather_briefing(condition)
    print(f"\n=== {condition} — {bundle.total_elapsed_s:.2f}s total ===")
    print(_json.dumps(bundle.manifest(), indent=2))
    for field_name in ("standard_of_care", "emerging_treatments", "approvals", "institutions"):
        r: SourceResult = getattr(bundle, field_name)
        print(f"\n-- {field_name} --")
        if r.usable:
            print(_json.dumps(r.data, indent=2)[:800])
        else:
            print(f"NOT USABLE: ok={r.ok} empty={r.empty} error_type={r.error_type} error={r.error}")
