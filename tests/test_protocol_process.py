import json
import queue
import subprocess
import sys
from pathlib import Path
import threading
import time
import tempfile

import pytest

from app_server import AppServerProvider, probe_candidate
from models import snapshot_from_result


def _fake_executable(tmp_path, mode="good"):
    script = tmp_path / f"fake_{mode}.py"
    script.write_text('''import json,sys\nif "--version" in sys.argv: print("codex-cli 9.9.9"); raise SystemExit\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get("method")=="initialize": print(json.dumps({"id":m["id"],"result":{}}),flush=True)\n elif m.get("method")=="account/rateLimits/read":\n  print(json.dumps({"id":m["id"],"result":({} if any("bad" in item for item in sys.argv) else {"rateLimits":{"limitId":"codex","primary":{"usedPercent":20}}})}),flush=True)\n elif m.get("method")=="crash": break\n''', encoding="utf-8")
    cmd = tmp_path / f"fake_{mode}.cmd"
    cmd.write_text(f'@echo off\n"{sys.executable}" "%~dp0{script.name}" %*\n', encoding="ascii")
    return cmd


class _FakeStdin:
    def __init__(self): self.payloads = []
    def write(self, value): self.payloads.append(value); return len(value)
    def flush(self): pass
    def close(self): pass


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin(); self.stdout = iter(()); self.stderr = iter(()); self._returncode = None
    def poll(self): return self._returncode
    def wait(self, timeout=None): self._returncode = 0; return 0
    def terminate(self): self._returncode = 0
    def kill(self): self._returncode = -9


def test_initialized_notification_is_send_only(monkeypatch):
    process = _FakeProcess(); provider = AppServerProvider(process_factory=lambda *a, **k: process)
    provider.stdin = process.stdin
    provider._send_raw({"method": "initialized"})
    assert json.loads(process.stdin.payloads[-1])["method"] == "initialized"


def test_reached_null_keeps_old_windows_but_schedules_full_refresh():
    provider = AppServerProvider(); provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}}, fetched_at=time.time() - 30)
    before = provider.snapshot.fetched_at
    provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "rateLimitReachedType": None}}})
    assert provider.snapshot.min_remaining == 80 and provider.snapshot.fetched_at == before and provider.refresh_needed


def test_reached_nonnull_is_authoritative_and_bumps_generation():
    provider = AppServerProvider(); provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    before = provider.snapshot.fetched_at
    provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "rateLimitReachedType": "primary"}}})
    assert provider.snapshot.buckets[0].reached_type == "primary"
    assert provider.snapshot.fetched_at >= before and provider.snapshot_generation == 1


def test_other_bucket_and_metadata_do_not_discard_full_response():
    provider = AppServerProvider(); provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    generation = provider.snapshot_generation
    provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "spark", "limitName": "metadata"}}})
    provider._handle_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "planType": "pro"}}})
    assert provider.snapshot_generation == generation and not provider.refresh_needed


def test_waiter_cycle_has_its_own_result_and_error():
    provider = AppServerProvider(); provider.start = lambda: None; provider._ensure = lambda deadline=None: None
    provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    entered = threading.Event(); release = threading.Event(); calls = []
    def request(method, **kwargs):
        calls.append(method); entered.set(); release.wait(1)
        return {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 21}}}}
    provider._request = request
    result = []
    leader = threading.Thread(target=lambda: result.append(provider.read(force=True, include_usage=False)))
    leader.start(); assert entered.wait(1)
    waiter = threading.Thread(target=lambda: result.append(provider.read(force=True, include_usage=False)))
    waiter.start(); time.sleep(0.03); release.set(); leader.join(); waiter.join()
    assert len(result) == 2 and calls.count("account/rateLimits/read") == 1


def test_completed_cycle_does_not_leak_result_into_next_cycle():
    provider = AppServerProvider(); provider.start = lambda: None; provider._ensure = lambda deadline=None: None
    provider.snapshot = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    calls = []; entered = threading.Event(); release = threading.Event()
    def request(method, **kwargs):
        calls.append(len(calls));
        if len(calls) == 1: entered.set(); release.wait(1)
        return {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20 + len(calls)}}}}
    provider._request = request
    first = []; t = threading.Thread(target=lambda: first.append(provider.read(force=True, include_usage=False))); t.start(); assert entered.wait(1)
    waiting = []; waiter = threading.Thread(target=lambda: waiting.append(provider.read(force=True, include_usage=False))); waiter.start(); time.sleep(0.03); release.set(); t.join(); waiter.join()
    second = provider.read(force=True, include_usage=False)
    assert len(calls) == 2 and len(first) == 1 and len(waiting) == 1 and first[0].min_remaining == waiting[0].min_remaining and first[0].min_remaining != second.min_remaining


def test_parse_activity_supports_bounded_official_dates():
    from models import parse_usage_activity
    activity = parse_usage_activity({"summary": {"startDate": "2026-01-01", "endDate": "2026-01-02"}, "secret": "no"})
    assert activity["summary"]["startDate"] == "2026-01-01" and "secret" not in activity


def test_activity_rejects_nested_numeric_and_string_identifiers():
    from models import parse_usage_activity
    activity = parse_usage_activity({"summary": {"tokens": 12, "accountId": 9988, "user_id": "user@example.com", "email": "user@example.com", "model": "gpt"}, "totalTokens": 12})
    assert activity["summary"]["tokens"] == 12 and activity["summary"]["model"] == "gpt"
    assert "accountId" not in activity["summary"] and "user_id" not in activity["summary"] and "email" not in activity["summary"]


def test_activity_walk_stops_at_budget():
    from collections.abc import Mapping
    from models import parse_usage_activity
    class CountingMap(Mapping):
        def __init__(self): self.count = 0; self.data = {str(i): i for i in range(5000)}
        def __iter__(self): return iter(self.data)
        def __len__(self): return len(self.data)
        def __getitem__(self, key): self.count += 1; return self.data[key]
    value = CountingMap(); parse_usage_activity({"summary": value})
    assert value.count <= 1024


def test_probe_candidate_real_fake_executable_send_only_initialized(tmp_path):
    candidate = _fake_executable(tmp_path)
    result = probe_candidate(candidate, timeout=3)
    assert result["ok"] and result["initialize"] and result["rateLimits"]


def test_provider_falls_back_to_next_fake_candidate(monkeypatch, tmp_path):
    import app_server
    bad = _fake_executable(tmp_path, "bad"); good = _fake_executable(tmp_path, "good")
    monkeypatch.setattr(app_server, "find_codex_executable", lambda deadline=None: (bad, []))
    monkeypatch.setattr(app_server, "find_candidates", lambda: [bad, good])
    provider = AppServerProvider(poll_seconds=999)
    provider._ensure(time.monotonic() + 5)
    assert provider.selected_candidate and provider.selected_candidate.casefold() == str(good).casefold()
    provider.stop()


def test_own_cache_fallback_has_explicit_source(tmp_path, monkeypatch):
    import app_server
    monkeypatch.setenv("CODEX_USAGE_GUARD_DATA_DIR", str(tmp_path))
    snap = snapshot_from_result({"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20}}})
    app_server.save_cache(snap, "proceed")
    loaded = app_server.load_fallback_cache()
    assert loaded is not None and loaded.source == "codex-usage-guard-cache"


def test_real_fake_jsonl_handshake_update_and_crash():
    code = r'''import json,sys
for line in sys.stdin:
    m=json.loads(line)
    if m.get("method")=="initialize": print(json.dumps({"id":m["id"],"result":{}}),flush=True)
    elif m.get("method")=="account/rateLimits/read":
        print(json.dumps({"method":"account/rateLimits/updated","params":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":30}}}}),flush=True)
        print(json.dumps({"id":m["id"],"result":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":30}}}}),flush=True)
    elif m.get("method")=="crash": break
'''
    proc = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    provider = AppServerProvider(); messages = queue.Queue(); provider.process = proc; provider.stdin = proc.stdin; provider.messages = messages
    reader = threading.Thread(target=provider._read_loop, args=(proc.stdout, messages), daemon=True); reader.start()
    provider._send_raw({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test"}}})
    assert provider._wait_response(1, timeout=1).get("id") == 1
    provider.next_id = 2
    provider._send_raw({"method": "initialized"})
    response = provider._request("account/rateLimits/read", timeout=1)
    assert response["result"]["rateLimits"]["primary"]["usedPercent"] == 30
    outcome = []
    def wait_request():
        try: provider._request("never", timeout=2)
        except Exception as exc: outcome.append(exc)
    pending = threading.Thread(target=wait_request); pending.start(); time.sleep(0.05)
    provider._send_raw({"id": 9, "method": "crash"}); pending.join(timeout=1)
    assert outcome and isinstance(outcome[0], RuntimeError)
    provider.stop(); reader.join(timeout=1)
