"""
Care Briefing — structured, cited condition briefings for a health system
strategy team.

Pipeline: an administrator asks about a condition in chat ->
sources.gather_briefing() fetches 4 public health-data APIs concurrently
(one fetch, shared) -> three Researcher agents (agents.py) each curate one
section from their own source stack in parallel, each with its own Claude
call, schema validation, and citation-grounding gate -> the verified
sections are assembled and rendered with live per-agent progress. If live
data is unusable across the board, a pre-curated corpus file for the seed
conditions is shown instead of a blank screen.

Same shape as towncrier's crew (specialist roles, cite-or-decline, collect
then publish), reimplemented in plain Python since there's no Claude Code
runtime behind a Streamlit app to dispatch subagents through.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import streamlit as st
from anthropic import Anthropic
from dotenv import load_dotenv

from agents import RESEARCHERS, assemble_briefing, extract_condition, run_all_researchers
from grounding import (
    BRIEFING_SCHEMA, SECTION_TITLES, SynthesisValidationError,
    assert_section_grounded, build_citation_index, validate_section,
)
from sources import BriefingBundle, gather_briefing

load_dotenv()

CORPUS_DIR = Path(__file__).parent / "corpus"
WALL_CLOCK_BUDGET_S = 15.0

DISCLAIMER = (
    "Informational briefing for internal strategy use, synthesized from "
    "public sources below — not clinical guidance."
)

STATUS_ICON = {"ok": "🟢", "empty": "⚪"}
EXAMPLE_CONDITIONS = [
    "Heart failure",
    "Type 2 diabetes",
    "Rheumatoid arthritis",
    "Multiple myeloma",
    "Psoriatic arthritis",
]


# --------------------------------------------------------------------------
# Whole-briefing validation (thin wrappers over grounding.py's per-section
# gates, kept so the assembled briefing has a single contract to check)
# --------------------------------------------------------------------------

def validate_briefing(data: dict) -> None:
    jsonschema.validate(instance=data, schema=BRIEFING_SCHEMA)
    titles = [s["title"] for s in data["sections"]]
    if set(titles) != set(SECTION_TITLES):
        raise SynthesisValidationError(f"expected sections {SECTION_TITLES}, got {titles}")
    for section in data["sections"]:
        validate_section(section)


def assert_citations_grounded(data: dict, allowed_citations: dict[str, str]) -> None:
    for section in data["sections"]:
        assert_section_grounded(section, allowed_citations)


# --------------------------------------------------------------------------
# Corpus fallback (seed conditions, curated earlier — not live data)
# --------------------------------------------------------------------------

@dataclass
class CorpusEntry:
    condition: str
    path: Path
    raw_markdown: str


def load_corpus() -> list[CorpusEntry]:
    entries = []
    if not CORPUS_DIR.exists():
        return entries
    for path in sorted(CORPUS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^condition:\s*"?([^"\n]+)"?\s*$', text, re.MULTILINE)
        condition = m.group(1).strip() if m else path.stem.replace("-", " ").title()
        entries.append(CorpusEntry(condition=condition, path=path, raw_markdown=text))
    return entries


def match_corpus(query: str, corpus: list[CorpusEntry], cutoff: float = 0.6) -> CorpusEntry | None:
    if not corpus:
        return None
    names = [e.condition for e in corpus]
    matches = difflib.get_close_matches(query, names, n=1, cutoff=cutoff)
    if not matches:
        return None
    return next(e for e in corpus if e.condition == matches[0])


def strip_frontmatter(raw_markdown: str) -> str:
    return re.sub(r"^---.*?---\s*", "", raw_markdown, count=1, flags=re.DOTALL)


def bundle_all_unusable(bundle: BriefingBundle) -> bool:
    return not any(
        r.usable
        for r in (bundle.standard_of_care, bundle.emerging_treatments, bundle.approvals, bundle.institutions)
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def manifest_lines(manifest: dict) -> list[str]:
    return [f"{STATUS_ICON.get(status, '🔴')} **{label}** — {status}" for label, status in manifest.items()]


def render_briefing(briefing: dict) -> None:
    st.markdown(f"### {briefing['condition']}")
    st.caption(
        "✅ Every claim below is grounded in a citation from a live source query "
        "for this condition, verified before display — nothing here is from the "
        "model's general knowledge."
    )
    references: dict[str, str] = {}
    for section in briefing["sections"]:
        status = section.get("status", "ok")
        icon = {"ok": "", "partial": "⚠️ ", "unavailable": "🚫 "}.get(status, "")
        with st.container(border=True):
            st.markdown(f"#### {icon}{section['title']}")
            if section.get("note"):
                st.caption(section["note"])
            if section.get("summary"):
                st.markdown(section["summary"])
            bullets = section.get("bullets", [])
            if bullets:
                st.markdown("**Evidence & sources**")
            for b in bullets:
                st.markdown(f"- {b['text']} [[{b['citation_label']}]]({b['citation_url']})")
                references[b["citation_label"]] = b["citation_url"]

    if references:
        with st.expander(f"References ({len(references)})"):
            for label, url in references.items():
                st.markdown(f"- [{label}]({url})")


def render_corpus_fallback(entry: CorpusEntry, reason: str) -> None:
    st.warning(
        f"{reason} Showing the cached reference briefing for **{entry.condition}** "
        "instead — curated earlier with inline citations, but not live data."
    )
    st.markdown(strip_frontmatter(entry.raw_markdown))


def render_result(result: dict) -> None:
    """Results are plain dicts so chat history replays without re-querying."""
    kind = result["kind"]
    if kind == "briefing":
        render_briefing(result["briefing"])
    elif kind == "corpus":
        render_corpus_fallback(result["entry"], result["reason"])
    else:
        st.error(result["message"])


# --------------------------------------------------------------------------
# Pipeline runner — one chat turn, with live per-agent progress
# --------------------------------------------------------------------------

def run_pipeline(query: str, client: Anthropic, corpus: list[CorpusEntry]) -> dict:
    with st.status("Reading your request…", expanded=True) as status:
        condition, recognized = extract_condition(query, client)
        if not recognized:
            status.update(label="I need a condition to research", state="error")
            return {
                "kind": "error",
                "message": (
                    "I couldn't find a medical condition in that message. Tell me which "
                    "condition you'd like a briefing on — for example: "
                    + ", ".join(EXAMPLE_CONDITIONS)
                ),
            }
        if condition.lower() != query.lower():
            st.write(f"Understood: **{condition}**")
        status.update(label=f"Researching **{condition}**…")
        query = condition

        st.write("Querying PubMed, ClinicalTrials.gov, openFDA, and NIH RePORTER in parallel…")
        bundle = gather_briefing(query, wall_clock_budget_s=WALL_CLOCK_BUDGET_S)
        st.write(f"Sources responded in {bundle.total_elapsed_s:.1f}s:")
        for line in manifest_lines(bundle.manifest()):
            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;{line}")

        if bundle_all_unusable(bundle):
            entry = match_corpus(query, corpus)
            if entry:
                status.update(label="Live sources unavailable — using cached reference", state="error")
                return {"kind": "corpus", "entry": entry, "reason": "No live data source returned usable results."}
            status.update(label="Nothing found for this condition", state="error")
            return {
                "kind": "error",
                "message": (
                    f"I couldn't find usable data for “{query}” in any live source, and it "
                    "isn't in the cached reference set. Try a different phrasing, or one of: "
                    + ", ".join(e.condition for e in corpus)
                ),
            }

        st.write(f"Dispatching {len(RESEARCHERS)} Researcher agents, one per section, in parallel:")
        for r in RESEARCHERS:
            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;🔎 **{r.title}** ← {r.stack_label}")

        sections: dict[str, dict] = {}
        for researcher, section, error in run_all_researchers(bundle, client):
            sections[researcher.title] = section
            if error:
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;🚫 **{researcher.title}** — declined: {error}")
            else:
                n = len(section["bullets"])
                st.markdown(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;✅ **{researcher.title}** — {n} claims, "
                    f"{n} citations verified against fetched sources"
                )

        briefing = assemble_briefing(query, sections)
        ok = sum(1 for s in briefing["sections"] if s["status"] != "unavailable")
        if ok == 0:
            status.update(label="All three Researchers declined", state="error", expanded=True)
        else:
            status.update(
                label=f"Briefing ready for **{query}** — {ok}/{len(SECTION_TITLES)} sections verified",
                state="complete", expanded=False,
            )

    return {"kind": "briefing", "briefing": briefing}


# --------------------------------------------------------------------------
# Chat UI
# --------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Care Briefing", page_icon="🩺", layout="centered")

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ORRERY_TENANT_DEFAULT_ANTHROPIC_API_KEY")
        or os.environ.get("ORRERY_TENANT_TOWNCRIER_ANTHROPIC_API_KEY")
    )
    if not api_key:
        st.error("No Anthropic API key found. Set ANTHROPIC_API_KEY (env var or .env file next to app.py).")
        st.stop()
    client = Anthropic(api_key=api_key)
    corpus = load_corpus()

    with st.sidebar:
        st.title("🩺 Care Briefing")
        st.markdown(
            "Ask about any medical condition and get a structured briefing: "
            "**current standard of care**, **emerging treatments in development**, "
            "and **key companies & institutions** — every claim cited to a live source."
        )
        st.markdown("**Try one:**")
        for example in EXAMPLE_CONDITIONS:
            if st.button(example, use_container_width=True):
                st.session_state["pending_query"] = example
        st.divider()
        st.caption(DISCLAIMER)
        st.caption("Sources: PubMed · ClinicalTrials.gov · openFDA · NIH RePORTER")
        if st.button("Clear conversation", use_container_width=True):
            st.session_state["messages"] = []
            st.rerun()

    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    if not st.session_state["messages"]:
        with st.chat_message("assistant"):
            st.markdown(
                "Hi — I prepare condition briefings for strategy and planning teams. "
                "**Which condition would you like a briefing on?**"
            )

    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                render_result(msg["content"])

    query = st.chat_input("Enter a condition, e.g. heart failure") or st.session_state.pop("pending_query", None)
    if not query:
        return

    query = query.strip()
    st.session_state["messages"].append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        result = run_pipeline(query, client, corpus)
        render_result(result)
    st.session_state["messages"].append({"role": "assistant", "content": result})


if __name__ == "__main__":
    main()
