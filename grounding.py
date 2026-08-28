"""
Shared trust layer: the section schema, the citation index built from raw
source data, and the two gates every model output must pass before it can
reach the client — structural validation and citation grounding.

Lives in its own module so the three Researcher agents (agents.py) and the
Streamlit app (app.py) use exactly the same rules; there is one definition
of "what counts as a valid, grounded section".
"""

from __future__ import annotations

import jsonschema

from sources import BriefingBundle

SECTION_TITLES = [
    "Standard of Care",
    "Emerging Treatments in Development",
    "Key Companies & Institutions",
]

# Bundle fields that carry a per-record URL vs. those that don't.
PUBMED = "standard_of_care"
TRIALS = "emerging_treatments"
APPROVALS = "approvals"
INSTITUTIONS = "institutions"
ALL_FIELDS = (PUBMED, TRIALS, APPROVALS, INSTITUTIONS)

SECTION_SCHEMA = {
    "type": "object",
    "required": ["title", "status", "bullets"],
    "properties": {
        "title": {"type": "string", "enum": SECTION_TITLES},
        "status": {"type": "string", "enum": ["ok", "partial", "unavailable"]},
        "summary": {
            "type": "string",
            "description": (
                "2-4 sentence plain-language synthesis of this section for a "
                "strategy leader, shown ABOVE the evidence bullets. Must only "
                "restate what the bullets establish — no facts that lack a bullet."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Required when status is partial/unavailable: which source is "
                "missing and why, in plain language for a strategy-team reader."
            ),
        },
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "citation_label", "citation_url"],
                "properties": {
                    "text": {"type": "string"},
                    "citation_label": {"type": "string"},
                    "citation_url": {"type": "string"},
                },
            },
        },
    },
}

BRIEFING_SCHEMA = {
    "type": "object",
    "required": ["condition", "sections"],
    "properties": {
        "condition": {"type": "string"},
        "sections": {"type": "array", "minItems": 3, "maxItems": 3, "items": SECTION_SCHEMA},
    },
}


class SynthesisValidationError(RuntimeError):
    """A model output that doesn't satisfy the contract. Callers treat it
    like any other synthesis failure — nothing that raises this is shown."""


def build_citation_index(bundle: BriefingBundle, fields: tuple[str, ...] = ALL_FIELDS) -> dict[str, str]:
    """The complete, real set of label -> URL pairs traceable to the given
    bundle fields — the only citations a Researcher may use, and the only
    ones assert_citations_grounded will accept. Two sources (openFDA labels,
    NIH RePORTER projects) carry no per-record URL, so they get one honest
    source-level citation rather than a fabricated deep link."""
    index: dict[str, str] = {}

    if PUBMED in fields and bundle.standard_of_care.usable:
        for a in bundle.standard_of_care.data:
            if a.get("url"):
                index[f"PubMed: {a.get('title') or a.get('pmid')}"] = a["url"]

    if TRIALS in fields and bundle.emerging_treatments.usable:
        for t in bundle.emerging_treatments.data.get("trials", []):
            if t.get("url"):
                index[f"ClinicalTrials.gov {t.get('nct_id')}: {t.get('title')}"] = t["url"]

    if APPROVALS in fields and bundle.approvals.usable:
        index["openFDA drug label database (indication search)"] = "https://api.fda.gov/drug/label.json"

    if INSTITUTIONS in fields and bundle.institutions.usable:
        index["NIH RePORTER (federal research project database)"] = "https://reporter.nih.gov/"

    return index


def slice_bundle(bundle: BriefingBundle, fields: tuple[str, ...]) -> dict:
    """The raw data for just the given fields, for a Researcher's prompt."""
    labels = {
        PUBMED: "pubmed_articles",
        TRIALS: "clinicaltrials_gov",
        APPROVALS: "openfda_approved_labels",
        INSTITUTIONS: "nih_reporter_projects",
    }
    out = {}
    for f in fields:
        r = getattr(bundle, f)
        out[labels[f]] = r.data if r.usable else None
    return out


def validate_section(section: dict) -> None:
    """Structural gate for one section, plus the rules JSON Schema can't
    express: a note whenever status isn't ok, and URL-shaped citations."""
    try:
        jsonschema.validate(instance=section, schema=SECTION_SCHEMA)
    except jsonschema.exceptions.ValidationError as exc:
        # Without this, a raw jsonschema.ValidationError escapes to the
        # generic except-Exception branch in agents.run_researcher, which
        # only reports the exception's class name ("ValidationError") and
        # throws away exc.message — the one piece of info that says what
        # the model actually got wrong. Reproduced live (7 real API calls
        # for one section, all passed) without hitting the schema failure
        # itself, so this couldn't be root-caused further this session —
        # wrapping it means the *next* occurrence is diagnosable instead
        # of just "ValidationError" with no detail, live or in a log.
        raise SynthesisValidationError(f"schema violation: {exc.message}") from exc
    if section["status"] != "ok" and not section.get("note"):
        raise SynthesisValidationError(
            f"section {section['title']!r} has status {section['status']!r} but no note"
        )
    for b in section.get("bullets", []):
        if not b["citation_url"].startswith(("http://", "https://")):
            raise SynthesisValidationError(
                f"bullet in {section['title']!r} has a non-URL citation: {b['citation_url']!r}"
            )


def assert_section_grounded(section: dict, allowed_citations: dict[str, str]) -> None:
    """The mechanical anti-hallucination check: every (label, url) pair the
    model returned must match, byte for byte, one we built from the real API
    responses. A plausible-but-invented URL fails; a real URL with the wrong
    label fails."""
    allowed_pairs = set(allowed_citations.items())
    for b in section.get("bullets", []):
        pair = (b["citation_label"], b["citation_url"])
        if pair not in allowed_pairs:
            raise SynthesisValidationError(
                f"ungrounded citation in {section['title']!r}: "
                f"{b['citation_label']!r} -> {b['citation_url']!r} "
                f"does not match any source actually fetched for this briefing"
            )
