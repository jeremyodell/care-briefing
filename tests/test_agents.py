"""
Tests for the three Researcher agents: stack ownership, per-agent citation
scoping, independent failure, and assembly. The Anthropic client is a fake —
no network, no API key needed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import agents  # noqa: E402
import grounding  # noqa: E402
import sources  # noqa: E402


def _bundle():
    return sources.BriefingBundle(
        query="q",
        normalized=sources.SourceResult(source="n", ok=True, data={}),
        standard_of_care=sources.SourceResult(
            source="PubMed", ok=True,
            data=[{"pmid": "111", "title": "A Guideline", "url": "https://pubmed.ncbi.nlm.nih.gov/111/"}],
        ),
        emerging_treatments=sources.SourceResult(
            source="ClinicalTrials.gov", ok=True,
            data={"trials": [{"nct_id": "NCT001", "title": "A Trial", "url": "https://clinicaltrials.gov/study/NCT001"}], "sponsors": ["Acme"]},
        ),
        approvals=sources.SourceResult(source="openFDA", ok=True, data=[{"brand_name": "X", "generic_name": "x", "manufacturer": None}]),
        institutions=sources.SourceResult(source="NIH RePORTER", ok=True, data={"projects": [], "institutions": ["Uni"]}),
    )


class FakeClient:
    """Returns whatever section dict it's told to, as a tool_use block."""
    def __init__(self, section=None, raise_exc=None):
        self._section = section
        self._raise = raise_exc
        self.calls = 0

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        if self._raise:
            raise self._raise
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self._section)])


def _by_key(key):
    return next(r for r in agents.RESEARCHERS if r.key == key)


# --------------------------------------------------------------------------
# Intake — natural language -> searchable condition
# --------------------------------------------------------------------------

def test_intake_extracts_condition_from_sentence():
    client = FakeClient({"condition": "prostate cancer", "is_condition": True})
    assert agents.extract_condition("i want a brief on prostate cancer", client) == ("prostate cancer", True)


def test_intake_reports_non_condition():
    client = FakeClient({"condition": "", "is_condition": False})
    cond, ok = agents.extract_condition("hello there", client)
    assert ok is False


def test_intake_falls_back_to_raw_text_on_api_failure():
    client = FakeClient(raise_exc=RuntimeError("down"))
    assert agents.extract_condition("heart failure", client) == ("heart failure", True)


# --------------------------------------------------------------------------
# Stack ownership
# --------------------------------------------------------------------------

def test_three_researchers_cover_exactly_the_three_sections():
    assert [r.title for r in agents.RESEARCHERS] == grounding.SECTION_TITLES


def test_each_researcher_only_sees_its_own_stack_citations():
    b = _bundle()
    soc = grounding.build_citation_index(b, _by_key("soc").fields)
    pipeline = grounding.build_citation_index(b, _by_key("pipeline").fields)
    players = grounding.build_citation_index(b, _by_key("players").fields)

    assert "https://api.fda.gov/drug/label.json" in soc.values()
    assert "https://reporter.nih.gov/" not in soc.values()          # SoC doesn't own RePORTER
    assert "https://clinicaltrials.gov/study/NCT001" in pipeline.values()
    assert "https://api.fda.gov/drug/label.json" not in pipeline.values()  # pipeline doesn't own openFDA
    assert "https://reporter.nih.gov/" in players.values()
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" not in players.values()  # players doesn't own PubMed


# --------------------------------------------------------------------------
# run_researcher — independent success / decline paths
# --------------------------------------------------------------------------

def test_grounded_section_passes_through():
    r = _by_key("pipeline")
    section = {
        "title": r.title, "status": "ok",
        "bullets": [{"text": "A trial is recruiting.", "citation_label": "ClinicalTrials.gov NCT001: A Trial",
                     "citation_url": "https://clinicaltrials.gov/study/NCT001"}],
    }
    out, err = agents.run_researcher(r, _bundle(), FakeClient(section))
    assert err is None
    assert out == section


def test_citation_from_another_agents_stack_is_rejected():
    # The SoC researcher tries to cite a ClinicalTrials.gov URL it was never
    # given — real URL, wrong stack -> ungrounded for THIS agent.
    r = _by_key("soc")
    section = {
        "title": r.title, "status": "ok",
        "bullets": [{"text": "x", "citation_label": "ClinicalTrials.gov NCT001: A Trial",
                     "citation_url": "https://clinicaltrials.gov/study/NCT001"}],
    }
    out, err = agents.run_researcher(r, _bundle(), FakeClient(section))
    assert out["status"] == "unavailable"
    assert "verification failed" in err


def test_wrong_title_is_rejected():
    r = _by_key("soc")
    section = {"title": "Emerging Treatments in Development", "status": "ok", "bullets": []}
    out, err = agents.run_researcher(r, _bundle(), FakeClient(section))
    assert out["title"] == r.title
    assert out["status"] == "unavailable"


def test_api_failure_becomes_honest_decline_not_raise():
    r = _by_key("players")
    out, err = agents.run_researcher(r, _bundle(), FakeClient(raise_exc=RuntimeError("boom")))
    assert out["status"] == "unavailable"
    assert "call failed" in err


def test_no_usable_sources_skips_the_model_call():
    b = _bundle()
    b.emerging_treatments = sources.SourceResult(source="ClinicalTrials.gov", ok=False, error_type="timeout")
    b.standard_of_care = sources.SourceResult(source="PubMed", ok=True, empty=True)
    client = FakeClient({"title": "x"})
    out, err = agents.run_researcher(_by_key("pipeline"), b, client)
    assert client.calls == 0
    assert out["status"] == "unavailable"
    assert err == "no usable sources"


# --------------------------------------------------------------------------
# Assembly / independence
# --------------------------------------------------------------------------

def test_assemble_orders_sections_and_fills_gaps():
    only_pipeline = {"Emerging Treatments in Development": {"title": "Emerging Treatments in Development", "status": "ok", "bullets": []}}
    briefing = agents.assemble_briefing("q", only_pipeline)
    assert [s["title"] for s in briefing["sections"]] == grounding.SECTION_TITLES
    assert briefing["sections"][0]["status"] == "unavailable"
    assert briefing["sections"][1]["status"] == "ok"
    assert briefing["sections"][2]["status"] == "unavailable"


def test_run_all_yields_every_researcher_even_when_one_fails(monkeypatch):
    def fake_run(r, bundle, client):
        if r.key == "soc":
            raise_exc = RuntimeError("down")
            return agents.unavailable_section(r.title, "down"), "call failed"
        return {"title": r.title, "status": "ok", "bullets": []}, None
    monkeypatch.setattr(agents, "run_researcher", fake_run)
    results = list(agents.run_all_researchers(_bundle(), FakeClient()))
    assert len(results) == 3
    statuses = {r.key: section["status"] for r, section, _ in results}
    assert statuses == {"soc": "unavailable", "pipeline": "ok", "players": "ok"}
