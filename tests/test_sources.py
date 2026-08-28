"""
Tests for sources.py's failure-classification logic. All network calls are
mocked (via monkeypatching sources._session.request) — nothing here touches
the real internet, so these run fast and deterministically, unlike the
python sources.py "<condition>" smoke test.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import sources  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Backoff/retry delays would make this suite slow for no reason —
    every test here is deterministic mocked I/O, not real network timing."""
    monkeypatch.setattr(sources.time, "sleep", lambda *_a, **_kw: None)


def _resp(status_code=200, json_data=None, json_raises=False, headers=None):
    r = Mock()
    r.status_code = status_code
    r.headers = headers or {}
    if json_raises:
        r.json.side_effect = ValueError("bad json")
    else:
        r.json.return_value = json_data
    return r


# --------------------------------------------------------------------------
# _request_json classification
# --------------------------------------------------------------------------

def test_200_success_returns_ok_with_data(monkeypatch):
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(200, {"x": 1}))
    r = sources._request_json("Test", "GET", "http://example.test")
    assert r.ok is True
    assert r.data == {"x": 1}
    assert r.usable is True


def test_429_retries_then_reports_rate_limited(monkeypatch):
    calls = []
    def fake(*a, **kw):
        calls.append(1)
        return _resp(429)
    monkeypatch.setattr(sources._session, "request", fake)
    r = sources._request_json("Test", "GET", "http://example.test", max_retries=1)
    assert r.ok is False
    assert r.error_type == "rate_limited"
    assert len(calls) == 2  # initial + 1 retry


def test_5xx_retries_then_reports_http_error(monkeypatch):
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(503))
    r = sources._request_json("Test", "GET", "http://example.test", max_retries=1)
    assert r.ok is False
    assert r.error_type == "http_error"


def test_4xx_does_not_retry(monkeypatch):
    calls = []
    def fake(*a, **kw):
        calls.append(1)
        return _resp(404)
    monkeypatch.setattr(sources._session, "request", fake)
    r = sources._request_json("Test", "GET", "http://example.test", max_retries=2)
    assert r.ok is False
    assert r.error_type == "http_error"
    assert len(calls) == 1  # no retry burned on a client-side/bad-query error


def test_unparseable_json_reported_as_http_error(monkeypatch):
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(200, json_raises=True))
    r = sources._request_json("Test", "GET", "http://example.test")
    assert r.ok is False
    assert r.error_type == "http_error"


def test_timeout_exception_classified_and_never_raises(monkeypatch):
    def raise_timeout(*a, **kw):
        raise __import__("requests").exceptions.Timeout()
    monkeypatch.setattr(sources._session, "request", raise_timeout)
    r = sources._request_json("Test", "GET", "http://example.test", max_retries=0)
    assert r.ok is False
    assert r.error_type == "timeout"


def test_connection_error_classified_as_network_error(monkeypatch):
    def raise_conn(*a, **kw):
        raise __import__("requests").exceptions.ConnectionError()
    monkeypatch.setattr(sources._session, "request", raise_conn)
    r = sources._request_json("Test", "GET", "http://example.test", max_retries=0)
    assert r.ok is False
    assert r.error_type == "network_error"


# --------------------------------------------------------------------------
# SourceResult.usable
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ok,empty,expected",
    [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
)
def test_usable_truth_table(ok, empty, expected):
    r = sources.SourceResult(source="x", ok=ok, empty=empty)
    assert r.usable is expected


# --------------------------------------------------------------------------
# BriefingBundle.manifest()
# --------------------------------------------------------------------------

def test_manifest_reports_ok_empty_and_unavailable_distinctly():
    bundle = sources.BriefingBundle(
        query="q",
        normalized=sources.SourceResult(source="n", ok=True, data={}),
        standard_of_care=sources.SourceResult(source="PubMed", ok=True, data=[1]),
        emerging_treatments=sources.SourceResult(source="ClinicalTrials.gov", ok=True, empty=True),
        approvals=sources.SourceResult(source="openFDA", ok=False, error_type="timeout"),
        institutions=sources.SourceResult(source="NIH RePORTER", ok=True, data={}),
    )
    m = bundle.manifest()
    assert m["standard of care (PubMed)"] == "ok"
    assert m["emerging treatments (ClinicalTrials.gov)"] == "empty"
    assert m["recent approvals (openFDA)"] == "unavailable (timeout)"


# --------------------------------------------------------------------------
# openFDA: null-manufacturer + missing-name record handling
# (the exact bug class dev-a8 flagged for the app.py synthesis layer)
# --------------------------------------------------------------------------

def test_openfda_keeps_record_with_null_manufacturer(monkeypatch):
    payload = {
        "results": [
            {"openfda": {"generic_name": ["DRUGX"], "brand_name": ["BrandX"]}},  # no manufacturer key
        ]
    }
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(200, payload))
    r = sources.fetch_openfda_approvals("condition")
    assert r.usable is True
    assert r.data[0]["manufacturer"] is None


def test_openfda_drops_record_with_no_name(monkeypatch):
    payload = {"results": [{"openfda": {}}]}
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(200, payload))
    r = sources.fetch_openfda_approvals("condition")
    assert r.empty is True


def test_openfda_404_treated_as_empty_not_unavailable(monkeypatch):
    monkeypatch.setattr(sources._session, "request", lambda *a, **kw: _resp(404))
    r = sources.fetch_openfda_approvals("nonexistent condition")
    assert r.ok is True
    assert r.empty is True


# --------------------------------------------------------------------------
# gather_briefing: wall-clock budget must not block on one slow source
# --------------------------------------------------------------------------

def test_gather_briefing_budget_caps_a_hanging_source(monkeypatch):
    def fast_ok(condition, limit=6):
        return sources.SourceResult(source="fast", ok=True, data=[1])

    def hangs(condition, limit=10):
        # threading.Event.wait(), not time.sleep() -- the autouse fixture
        # above monkeypatches sources.time.sleep, and sources.time IS this
        # test module's `time` (same cached module object), so a
        # time.sleep() "hang" here would silently no-op too.
        threading.Event().wait(5)
        return sources.SourceResult(source="slow", ok=True, data=[1])

    monkeypatch.setattr(sources, "normalize_condition", lambda q: sources.SourceResult(source="n", ok=True, data={}))
    monkeypatch.setattr(sources, "fetch_pubmed", fast_ok)
    monkeypatch.setattr(sources, "fetch_clinicaltrials", hangs)
    monkeypatch.setattr(sources, "fetch_openfda_approvals", fast_ok)
    monkeypatch.setattr(sources, "fetch_reporter", fast_ok)

    start = time.monotonic()
    bundle = sources.gather_briefing("condition", wall_clock_budget_s=0.3)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0  # did not wait out the 5s hang
    assert bundle.standard_of_care.usable is True
    assert bundle.emerging_treatments.ok is False
    assert bundle.emerging_treatments.error_type == "timeout"
