import io
import json
import threading
import time

from app_server import AppServerProvider, RefreshCycle, redact
from models import snapshot_from_result
from policy import evaluate


def test_background_poll_refreshes_hook_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_USAGE_GUARD_DATA_DIR", str(tmp_path))
    provider = AppServerProvider()
    provider.start = lambda: None
    provider._ensure = lambda deadline=None: None
    provider._request = lambda *args, **kwargs: {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 26}}}}
    monkeypatch.setattr(provider.stop_event, "wait", lambda timeout: provider.stop_event.set())
    provider._poll_loop()
    cached = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert cached["fetchedAt"] == provider.snapshot.fetched_at
    assert cached["minRemainingPercent"] == 74 and cached["decision"] == "proceed"


def test_inflight_notifications_preserve_full_response_windows():
    provider = AppServerProvider()
    provider.start = lambda: None
    provider._ensure = lambda deadline=None: None

    def request(*args, **kwargs):
        for used in (15, 20):
            provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": used}}}})
        return {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10, "windowDurationMins": 300}, "secondary": {"usedPercent": 95}}}}

    provider._request = request
    snapshot = provider.read(force=True, include_usage=False)
    bucket = snapshot.buckets[0]
    assert bucket.primary.used_percent == 20 and bucket.primary.window_duration_mins == 300
    assert bucket.secondary is not None and bucket.secondary.used_percent == 95
    assert evaluate(snapshot)["decision"] == "critical"


def test_notification_captures_cycle_before_completion():
    class CompletingProvider(AppServerProvider):
        def __getattribute__(self, name):
            value = super().__getattribute__(name)
            if name == "refresh_cycle":
                self.refresh_cycle = None
            return value

    provider = CompletingProvider()
    cycle = RefreshCycle()
    provider.refresh_cycle = cycle
    params = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}}
    provider._handle_notification({"method": "account/rateLimits/updated", "params": params})
    assert cycle.notifications == [params]


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
    stream = io.StringIO("")
    provider._read_loop(stream, provider.messages)
    assert event.wait(0.2) and "error" in result and stream.closed


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
