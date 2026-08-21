import math

import pytest

from models import UsagePayloadError, snapshot_from_result, merge_notification, snapshot_from_cache
from policy import evaluate


def payload(limit_id="codex", used=20):
    return {"limitId": limit_id, "primary": {"usedPercent": used, "windowDurationMins": 60, "resetsAt": 4102444800}}


def test_float_is_accepted_and_clamped():
    snap = snapshot_from_result({"rateLimits": payload(used=120.5)})
    assert snap.min_remaining == 0


@pytest.mark.parametrize(("used", "remaining"), [(-1, 100), (101, 0)])
def test_user_approved_used_percent_clamp(used, remaining):
    assert snapshot_from_result({"rateLimits": payload(used=used)}).min_remaining == remaining


@pytest.mark.parametrize("value", [True, "20", float("nan"), float("inf")])
def test_bad_used_percent_rejected(value):
    with pytest.raises(UsagePayloadError): snapshot_from_result({"rateLimits": payload(used=value)})


def test_other_bucket_cannot_make_missing_codex_proceed():
    snap = snapshot_from_result({"rateLimitsByLimitId": {"spark": payload("spark", 1)}})
    assert snap.min_remaining is None
    assert evaluate(snap, now=snap.fetched_at)["decision"] == "unknown"


@pytest.mark.parametrize(("used", "decision"), [(20, "proceed"), (65, "watch"), (80, "checkpoint"), (91, "critical")])
def test_fixed_policy(used, decision):
    snap = snapshot_from_result({"rateLimits": payload(used=used)}, fetched_at=100)
    assert evaluate(snap, now=100)["decision"] == decision


@pytest.mark.parametrize(("remaining", "decision"), [(40.01, "proceed"), (40, "watch"), (25.01, "watch"), (25, "checkpoint"), (10.01, "checkpoint"), (10, "critical")])
def test_exact_edges(remaining, decision):
    snap = snapshot_from_result({"rateLimits": payload(used=100 - remaining)}, fetched_at=100)
    result = evaluate(snap, now=100)
    assert result["decision"] == decision
    assert result["allowDelegation"] is (decision == "proceed")


def test_stale_boundary_is_unknown():
    snap = snapshot_from_result({"rateLimits": payload(used=20)}, fetched_at=100)
    assert evaluate(snap, now=220)["decision"] == "unknown"


def test_sparse_nullable_fields_clear_and_future_cache_rejected():
    snap = snapshot_from_result({"rateLimits": {**payload(), "credits": {"hasCredits": True, "balance": "1"}, "planType": "pro"}}, fetched_at=100)
    merged = merge_notification({"rateLimits": {"limitId": "codex", "credits": None, "planType": None}}, snap)
    assert merged.buckets[0].credits is None and merged.buckets[0].plan_type is None
    with pytest.raises(UsagePayloadError): snapshot_from_cache({"schemaVersion": 1, "fetchedAt": 10**12, "buckets": [payload()]})
