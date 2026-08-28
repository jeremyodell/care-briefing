"""
Tests for app.py's client-facing safety layer: schema validation on the
model's output (the exact thing Jeremy asked to see tested before touching
the UI), plus the pure helper functions around it.
"""

import sys
from pathlib import Path

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import app  # noqa: E402
import sources  # noqa: E402


def _valid_briefing():
    return {
        "condition": "Rheumatoid Arthritis",
        "sections": [
            {
                "title": "Standard of Care",
                "status": "ok",
                "bullets": [
                    {"text": "Methotrexate is first-line therapy.", "citation_label": "ACR Guideline", "citation_url": "https://pubmed.ncbi.nlm.nih.gov/123/"}
                ],
            },
            {
                "title": "Emerging Treatments in Development",
                "status": "ok",
                "bullets": [
                    {"text": "A Phase III JAK inhibitor trial is recruiting.", "citation_label": "NCT00000000", "citation_url": "https://clinicaltrials.gov/study/NCT00000000"}
                ],
            },
            {
                "title": "Key Companies & Institutions",
                "status": "unavailable",
                "note": "NIH RePORTER did not respond within the time budget.",
                "bullets": [],
            },
        ],
    }


# --------------------------------------------------------------------------
# validate_briefing — this is the gate the client now trusts
# --------------------------------------------------------------------------

def test_valid_briefing_passes():
    app.validate_briefing(_valid_briefing())  # must not raise


def test_missing_citation_url_fails_schema():
    data = _valid_briefing()
    del data["sections"][0]["bullets"][0]["citation_url"]
    with pytest.raises(jsonschema.exceptions.ValidationError):
        app.validate_briefing(data)


def test_bad_status_enum_fails_schema():
    data = _valid_briefing()
    data["sections"][0]["status"] = "probably fine"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        app.validate_briefing(data)


def test_wrong_section_titles_rejected():
    data = _valid_briefing()
    data["sections"][0]["title"] = "Emerging Treatments in Development"  # now a duplicate; "Standard of Care" missing
    with pytest.raises(app.SynthesisValidationError):
        app.validate_briefing(data)


def test_non_ok_status_without_note_rejected():
    data = _valid_briefing()
    data["sections"][2]["note"] = ""
    with pytest.raises(app.SynthesisValidationError):
        app.validate_briefing(data)


def test_non_url_citation_rejected():
    data = _valid_briefing()
    data["sections"][0]["bullets"][0]["citation_url"] = "see the ACR guideline"
    with pytest.raises(app.SynthesisValidationError):
        app.validate_briefing(data)


def test_wrong_number_of_sections_fails_schema():
    data = _valid_briefing()
    data["sections"] = data["sections"][:2]
    with pytest.raises(jsonschema.exceptions.ValidationError):
        app.validate_briefing(data)


# --------------------------------------------------------------------------
# build_citation_index / assert_citations_grounded — the anti-hallucination
# gate Jeremy asked for: every returned citation must be one we actually
# fetched, not merely something that looks like a URL.
# --------------------------------------------------------------------------

def _bundle_with_real_sources():
    return sources.BriefingBundle(
        query="q",
        normalized=sources.SourceResult(source="n", ok=True, data={}),
        standard_of_care=sources.SourceResult(
            source="PubMed", ok=True,
            data=[{"pmid": "111", "title": "A Guideline", "url": "https://pubmed.ncbi.nlm.nih.gov/111/"}],
        ),
        emerging_treatments=sources.SourceResult(
            source="ClinicalTrials.gov", ok=True,
            data={"trials": [{"nct_id": "NCT001", "title": "A Trial", "url": "https://clinicaltrials.gov/study/NCT001"}], "sponsors": []},
        ),
        approvals=sources.SourceResult(source="openFDA", ok=True, data=[{"brand_name": "X", "generic_name": "x", "manufacturer": None}]),
        institutions=sources.SourceResult(source="NIH RePORTER", ok=True, data={"projects": [], "institutions": []}),
    )


def test_citation_index_includes_real_per_item_urls():
    index = app.build_citation_index(_bundle_with_real_sources())
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in index.values()
    assert "https://clinicaltrials.gov/study/NCT001" in index.values()


def test_citation_index_uses_honest_source_level_url_when_no_per_item_url():
    index = app.build_citation_index(_bundle_with_real_sources())
    # openFDA/NIH RePORTER carry no per-record URL in the raw data -- the
    # index must not fabricate one.
    assert "https://api.fda.gov/drug/label.json" in index.values()
    assert "https://reporter.nih.gov/" in index.values()


def test_citation_index_omits_unusable_sources():
    bundle = _bundle_with_real_sources()
    bundle.approvals = sources.SourceResult(source="openFDA", ok=False, error_type="timeout")
    index = app.build_citation_index(bundle)
    assert "https://api.fda.gov/drug/label.json" not in index.values()


def test_grounded_citation_passes():
    bundle = _bundle_with_real_sources()
    index = app.build_citation_index(bundle)
    data = _valid_briefing()
    label, url = next(iter(index.items()))
    data["sections"][0]["bullets"][0]["citation_label"] = label
    data["sections"][0]["bullets"][0]["citation_url"] = url
    # _valid_briefing()'s other sections carry their own fixture citations
    # that aren't in this bundle's index -- clear them so this test checks
    # exactly one thing (the grounded bullet passes), not an unrelated one.
    data["sections"][1]["bullets"] = []
    app.assert_citations_grounded(data, index)  # must not raise


def test_fabricated_url_is_rejected_even_if_well_formed():
    bundle = _bundle_with_real_sources()
    index = app.build_citation_index(bundle)
    data = _valid_briefing()
    # A plausible-looking but never-fetched PubMed URL -- exactly the
    # hallucination this check exists to catch.
    data["sections"][0]["bullets"][0]["citation_label"] = "PubMed: A Guideline"
    data["sections"][0]["bullets"][0]["citation_url"] = "https://pubmed.ncbi.nlm.nih.gov/9999999/"
    with pytest.raises(app.SynthesisValidationError):
        app.assert_citations_grounded(data, index)


def test_real_url_with_mismatched_label_is_rejected():
    bundle = _bundle_with_real_sources()
    index = app.build_citation_index(bundle)
    data = _valid_briefing()
    real_url = "https://pubmed.ncbi.nlm.nih.gov/111/"
    data["sections"][0]["bullets"][0]["citation_label"] = "PubMed: A Totally Different Article"
    data["sections"][0]["bullets"][0]["citation_url"] = real_url
    with pytest.raises(app.SynthesisValidationError):
        app.assert_citations_grounded(data, index)


# --------------------------------------------------------------------------
# bundle_all_unusable
# --------------------------------------------------------------------------

def _bundle(**overrides):
    base = dict(
        query="q",
        normalized=sources.SourceResult(source="n", ok=True, data={}),
        standard_of_care=sources.SourceResult(source="PubMed", ok=False, error_type="timeout"),
        emerging_treatments=sources.SourceResult(source="ClinicalTrials.gov", ok=False, error_type="timeout"),
        approvals=sources.SourceResult(source="openFDA", ok=False, error_type="timeout"),
        institutions=sources.SourceResult(source="NIH RePORTER", ok=False, error_type="timeout"),
    )
    base.update(overrides)
    return sources.BriefingBundle(**base)


def test_all_unusable_true_when_every_source_failed():
    assert app.bundle_all_unusable(_bundle()) is True


def test_all_unusable_false_when_one_source_usable():
    b = _bundle(standard_of_care=sources.SourceResult(source="PubMed", ok=True, data=[1]))
    assert app.bundle_all_unusable(b) is False


def test_all_unusable_true_when_only_empty_not_error():
    # queried fine, found nothing -> still not "usable" data to synthesize from
    b = _bundle(standard_of_care=sources.SourceResult(source="PubMed", ok=True, empty=True))
    assert app.bundle_all_unusable(b) is True


# --------------------------------------------------------------------------
# corpus loading / matching / frontmatter stripping
# --------------------------------------------------------------------------

def test_load_corpus_finds_seed_conditions():
    corpus = app.load_corpus()
    names = {e.condition for e in corpus}
    assert "Type 2 Diabetes" in names
    assert len(corpus) == 4


def test_match_corpus_exact_name():
    corpus = app.load_corpus()
    entry = app.match_corpus("Type 2 Diabetes", corpus)
    assert entry is not None
    assert entry.condition == "Type 2 Diabetes"


def test_match_corpus_no_match_returns_none():
    corpus = app.load_corpus()
    entry = app.match_corpus("completely unrelated nonsense condition xyz", corpus)
    assert entry is None


def test_strip_frontmatter_removes_yaml_block():
    raw = "---\ncondition: X\n---\n\n# Body\ncontent here"
    stripped = app.strip_frontmatter(raw)
    assert "condition:" not in stripped
    assert "# Body" in stripped
