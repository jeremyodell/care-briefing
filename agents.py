"""
The three Researcher agents. Each owns exactly one section of the briefing
and one stack of sources, and makes its own independent curation call:

    fetch slice -> own citation index -> own Claude call (single-section
    schema) -> own validation + grounding gate -> verified section

They run concurrently and fail independently: one Researcher that can't
produce a grounded section reports "unavailable" with an honest note, and
the other two still ship. Same shape as towncrier's crew, in plain Python.

Data is fetched once (sources.gather_briefing) and each Researcher is
handed only its slice — no duplicate API calls during the demo.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import jsonschema
from dataclasses import dataclass
from typing import Callable, Iterator

from anthropic import Anthropic

from grounding import (
    ALL_FIELDS, APPROVALS, INSTITUTIONS, PUBMED, SECTION_SCHEMA, SECTION_TITLES, TRIALS,
    SynthesisValidationError, assert_section_grounded, build_citation_index, slice_bundle,
    validate_section,
)
from sources import BriefingBundle

MODEL = "claude-sonnet-4-5-20250929"


@dataclass(frozen=True)
class Researcher:
    key: str
    title: str                 # the section it owns (one of SECTION_TITLES)
    fields: tuple[str, ...]    # its source stack, as BriefingBundle field names
    stack_label: str           # human-readable stack, for the progress panel
    focus: str                 # role-specific guidance appended to the shared prompt


RESEARCHERS: list[Researcher] = [
    Researcher(
        key="soc",
        title="Standard of Care",
        fields=(PUBMED, APPROVALS),
        stack_label="PubMed guidelines/reviews + openFDA approved labels",
        focus=(
            "Describe what is established, guideline-backed, or FDA-labeled today. "
            "Prefer guideline and systematic-review evidence over generic drug-label "
            "matches; a label match alone is weak evidence of standard of care and "
            "should be framed as such."
        ),
    ),
    Researcher(
        key="pipeline",
        title="Emerging Treatments in Development",
        fields=(TRIALS, PUBMED),
        stack_label="ClinicalTrials.gov active trials + PubMed investigational reviews",
        focus=(
            "Describe what is in development: name the agent, its mechanism if "
            "stated, the trial phase, status, and sponsor. Only trials and "
            "investigational reviews in the data — nothing already standard of care."
        ),
    ),
    Researcher(
        key="players",
        title="Key Companies & Institutions",
        fields=(TRIALS, APPROVALS, INSTITUTIONS),
        stack_label="ClinicalTrials.gov sponsors + openFDA manufacturers + NIH RePORTER",
        focus=(
            "Name the companies sponsoring trials, manufacturers of labeled drugs, "
            "and institutions holding federal research funding — and what each is "
            "doing. Group by organization; skip any with no clear role in the data."
        ),
    ),
]

SECTION_TOOL = {
    "name": "emit_section",
    "description": "Emit one verified section of the condition briefing.",
    "input_schema": SECTION_SCHEMA,
}

SYSTEM_PROMPT = """You are one Researcher in a small evidence-synthesis crew \
preparing a condition briefing for a health system strategy team. You own \
exactly ONE section: "{title}". You are given raw data already fetched from \
your source stack ({stack}), plus an "allowed_citations" list of label/url \
pairs.

Hard rules, non-negotiable:
- Ground every claim. Every bullet's citation_label and citation_url MUST be \
copied VERBATIM from one entry in allowed_citations — never alter, shorten, \
combine, or construct a URL, even a plausible one. allowed_citations is the \
complete and only set you may cite; a claim you cannot tie to an entry does \
not get a bullet. This is checked mechanically after you respond.
- Cite or decline. Never add a claim, company, trial, or statistic that is \
not present in the provided data, even if you believe it true from general \
knowledge.
- If a source in your stack is null (unavailable or empty), set status to \
"partial" (some data) or "unavailable" (none), write a short honest note \
saying what's missing, and only write bullets the data supports.
- Write for a health system strategy team: plain language, decision-relevant, \
no unexplained jargon. Omit null fields silently rather than writing "None".
- At most 8 bullets, one sentence each. Prefer the most decision-relevant \
items over completeness.
- ALWAYS write a "summary": 2-4 sentences a strategy leader can read straight \
through — what the picture is, what matters, what's changing. It is shown \
above the bullets. It may only restate what your bullets establish; any \
fact in the summary must have a cited bullet beneath it.
- Section focus: {focus}

Call the emit_section tool with title exactly "{title}". No prose outside \
the tool call."""


# --------------------------------------------------------------------------
# Intake — turn whatever the administrator typed into a searchable condition
# --------------------------------------------------------------------------

INTAKE_TOOL = {
    "name": "extract_condition",
    "description": "Extract the medical condition the user wants a briefing on.",
    "input_schema": {
        "type": "object",
        "required": ["condition", "is_condition"],
        "properties": {
            "condition": {
                "type": "string",
                "description": (
                    "The condition name only, as it would appear in a medical "
                    "database (e.g. 'prostate cancer', 'heart failure'). Correct "
                    "obvious misspellings. Empty string if none is present."
                ),
            },
            "is_condition": {
                "type": "boolean",
                "description": "False if the message does not name a medical condition.",
            },
        },
    },
}

INTAKE_PROMPT = (
    "You are the Intake step of a condition-briefing tool for health system "
    "administrators. The user types in natural language ('i want a brief on "
    "prostate cancer', 'what's new in HFpEF?', 'RA'). Extract just the condition "
    "name in standard medical form, expanding common abbreviations and fixing "
    "obvious misspellings. Do not add anything the user didn't ask about. Call "
    "extract_condition."
)


def extract_condition(message: str, client: Anthropic) -> tuple[str, bool]:
    """Returns (condition, recognized). On any failure, falls back to the raw
    message so a hiccup here never blocks a search that might still work."""
    text = message.strip()
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=INTAKE_PROMPT,
            tools=[INTAKE_TOOL],
            tool_choice={"type": "tool", "name": "extract_condition"},
            messages=[{"role": "user", "content": text}],
        )
        out = next((b.input for b in resp.content if b.type == "tool_use"), None)
        if not out:
            return text, True
        condition = (out.get("condition") or "").strip()
        if not out.get("is_condition") or not condition:
            return text, False
        return condition, True
    except Exception:  # noqa: BLE001 — intake is best-effort, never fatal
        return text, True


def unavailable_section(title: str, note: str) -> dict:
    return {"title": title, "status": "unavailable", "note": note, "bullets": []}


# Retries for a verification failure specifically (bad structured output on
# an otherwise-successful call), not for transport failures — the Anthropic
# SDK already retries those itself (connection errors, 408/409/429/5xx, its
# own default max_retries=2) before this function ever sees them. What
# wasn't covered: the model returning a 200 with output that fails our own
# schema/grounding checks — a semantic failure the SDK has no way to know
# about. Live-tested this specific gap: 1 real failure seen (the case Jeremy
# hit), then 7/7 fresh attempts for the same section/condition passed clean
# — nondeterministic model-output noise, not a systemic bug, so a cheap
# retry recovers most of it. Capped at 2 total attempts so a persistently
# bad prompt/data combination still fails fast instead of looping.
MAX_SYNTHESIS_ATTEMPTS = 2


def run_researcher(researcher: Researcher, bundle: BriefingBundle, client: Anthropic) -> tuple[dict, str | None]:
    """One independent curation call. Returns (section, error) — the section
    is always a valid, renderable dict; error is a short reason when the
    Researcher had to decline (no data / failed validation / API failure)."""
    citations = build_citation_index(bundle, researcher.fields)
    if not citations:
        return unavailable_section(
            researcher.title,
            f"None of this section's sources ({researcher.stack_label}) returned usable data.",
        ), "no usable sources"

    context = {
        "condition": bundle.query,
        "section": researcher.title,
        "data": slice_bundle(bundle, researcher.fields),
        "allowed_citations": [{"citation_label": k, "citation_url": v} for k, v in citations.items()],
    }
    system = SYSTEM_PROMPT.format(title=researcher.title, stack=researcher.stack_label, focus=researcher.focus)

    last_verification_error: SynthesisValidationError | None = None
    for attempt in range(1, MAX_SYNTHESIS_ATTEMPTS + 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system,
                tools=[SECTION_TOOL],
                tool_choice={"type": "tool", "name": "emit_section"},
                messages=[{"role": "user", "content": json.dumps(context, indent=2)}],
            )
            if getattr(resp, "stop_reason", None) == "max_tokens":
                raise SynthesisValidationError("output truncated at max_tokens — section too long")
            section = next((b.input for b in resp.content if b.type == "tool_use"), None)
            if section is None:
                raise SynthesisValidationError("model did not call emit_section")
            if section.get("title") != researcher.title:
                raise SynthesisValidationError(f"wrong section title {section.get('title')!r}")
            try:
                validate_section(section)
            except jsonschema.ValidationError as exc:
                raise SynthesisValidationError(f"schema: {exc.message}") from exc
            assert_section_grounded(section, citations)
            return section, None
        except SynthesisValidationError as exc:
            last_verification_error = exc
            continue  # a fresh sample from the model is likely to just work — retry before giving up
        except Exception as exc:  # noqa: BLE001 — API-level failure the SDK's own retries couldn't recover
            return unavailable_section(
                researcher.title, f"The synthesis call for this section failed ({exc.__class__.__name__})."
            ), f"call failed: {exc.__class__.__name__}"

    return unavailable_section(
        researcher.title,
        f"The synthesized section failed citation/schema verification on {MAX_SYNTHESIS_ATTEMPTS} attempts "
        f"and was withheld (last error: {last_verification_error}).",
    ), f"verification failed after {MAX_SYNTHESIS_ATTEMPTS} attempts: {last_verification_error}"


def run_all_researchers(
    bundle: BriefingBundle, client: Anthropic
) -> Iterator[tuple[Researcher, dict, str | None]]:
    """Fan the three Researchers out concurrently; yield each as it finishes
    so the caller can show live per-agent progress."""
    with ThreadPoolExecutor(max_workers=len(RESEARCHERS)) as pool:
        futures = {pool.submit(run_researcher, r, bundle, client): r for r in RESEARCHERS}
        for fut in as_completed(futures):
            r = futures[fut]
            section, error = fut.result()
            yield r, section, error


def assemble_briefing(condition: str, sections: dict[str, dict]) -> dict:
    """Put sections in canonical order; any missing one is an honest gap."""
    ordered = [
        sections.get(t) or unavailable_section(t, "This section's Researcher did not return a result.")
        for t in SECTION_TITLES
    ]
    return {"condition": condition, "sections": ordered}
