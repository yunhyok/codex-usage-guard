import threading
import time

from app_server import AppServerProvider, redact
from models import snapshot_from_result


def test_fake_jsonl_response_releases_waiter():
    provider = AppServerProvider()
    event = threading.Event(); result = {}
    provider.pending[7] = (event, result)
    provider._handle_notification({"id": 7, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}}})
    assert event.is_set() and result["id"] == 7


def test_fake_updated_notification_merges_sparse_window():
    provider = AppServerProvider()
    provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 60}}})
    provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 30}}}})
    assert provider.snapshot.buckets[0].primary.window_duration_mins == 60
    assert provider.snapshot.buckets[0].primary.used_percent == 30


def test_fake_server_diagnostics_do_not_leak_secret():
    value = redact("Bearer abc token=secret user@example.com")
    assert "abc" not in value and "secret" not in value and "@" not in value
    bare_key = "sk-" + "proj-" + ("x" * 20)
    assert bare_key not in redact(bare_key)


def test_eof_fails_pending_waiter_immediately():
    provider = AppServerProvider()
    event = threading.Event(); result = {}
    provider.pending[9] = (event, result); provider.messages = __import__("queue").Queue()
    provider._read_loop(iter(()), provider.messages)
    assert event.wait(0.2) and "error" in result


def test_overlapping_force_refreshes_coalesce(monkeypatch):
    provider = AppServerProvider()
    provider.start = lambda: None
    provider._ensure = lambda deadline=None: None
    provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    calls = {"count": 0}
    def request(method, timeout=None, deadline=None):
        calls["count"] += 1; time.sleep(0.05)
        return {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}}}
    provider._request = request
    results = []
    threads = [threading.Thread(target=lambda: results.append(provider.read(force=True, include_usage=False))) for _ in range(3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert calls["count"] == 1 and len(results) == 3
