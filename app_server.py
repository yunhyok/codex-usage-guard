from __future__ import annotations

from collections import deque
import atexit
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import tempfile
from typing import Any
from dataclasses import dataclass, field

from models import Snapshot, UsagePayloadError, merge_notification, parse_usage_activity, snapshot_from_cache, snapshot_from_result

APP_VERSION = "0.1.1"
APP_NAME = "Codex Usage Guard"
POLL_SECONDS = 60.0
STALE_SECONDS = 120.0
_secret = re.compile(r"(?i)(bearer\s+|[\"']?(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|token|authorization|chatgptAccountId|account[_ -]?id|user[_ -]?id|workspace[_ -]?id|organization[_ -]?id)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)")
_email = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_uuid = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
_bare_secret = re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}")


@dataclass
class RefreshCycle:
    event: threading.Event = field(default_factory=threading.Event)
    result: Snapshot | None = None
    error: Exception | None = None
    include_activity: bool = False


def redact(value: str) -> str:
    value = _secret.sub(r"\1<redacted>", value)
    value = _email.sub("<redacted-email>", value)
    value = _uuid.sub("<redacted-id>", value)
    value = _bare_secret.sub("<redacted-secret>", value)
    return value.strip()[-1000:]


def _version(text: str) -> tuple[int, ...]:
    match = re.search(r"(?:codex-cli\s+|v)(\d+(?:\.\d+)+)", text, re.I)
    return tuple(int(x) for x in match.group(1).split(".")) if match else (0,)


def find_candidates() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("CODEX_USAGE_GUARD_CODEX", "").strip()
    if override: paths.append(Path(override))
    found = shutil.which("codex")
    if found: paths.append(Path(found))
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    paths.append(base / "codex.exe")
    if base.is_dir():
        for child in base.iterdir():
            if child.is_dir(): paths.append(child / "codex.exe")
    return list(dict.fromkeys(p for p in paths if p.is_file()))


def find_codex_executable(*, deadline: float | None = None) -> tuple[Path | None, list[str]]:
    diagnostics: list[str] = []
    override = os.environ.get("CODEX_USAGE_GUARD_CODEX", "").strip()
    if override and Path(override).is_file():
        return Path(override).resolve(), diagnostics
    ranked: list[tuple[tuple[int, ...], Path]] = []
    for path in find_candidates():
        if deadline is not None and time.monotonic() >= deadline: break
        try:
            remaining = 5.0 if deadline is None else max(0.01, deadline - time.monotonic())
            completed = subprocess.run([str(path), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=min(5.0, remaining), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (completed.stdout or "") + " " + (completed.stderr or "")
            if completed.returncode == 0 and _version(text) != (0,): ranked.append((_version(text), path))
            else: diagnostics.append(f"{path.name}: handshake rejected ({redact(text)})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostics.append(f"{path.name}: {redact(str(exc))}")
    ranked.sort(key=lambda item: item[0], reverse=True)
    return (ranked[0][1] if ranked else None), diagnostics


def probe_candidate(path: Path, timeout: float = 15.0, *, deadline: float | None = None) -> dict[str, Any]:
    """Perform a bounded, credential-free initialize + rateLimits probe."""
    started = time.monotonic(); absolute_deadline = deadline if deadline is not None else started + timeout
    result: dict[str, Any] = {"candidate": str(path), "version": None, "initialize": False, "rateLimits": False, "ok": False}
    try:
        remaining = max(0.01, absolute_deadline - time.monotonic())
        vp = subprocess.run([str(path), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=min(5.0, remaining), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (vp.stdout or "") + " " + (vp.stderr or "")
        result["version"] = ".".join(str(x) for x in _version(text)) if _version(text) != (0,) else None
        if vp.returncode != 0 or result["version"] is None:
            result["error"] = "version rejected"; return result
        if time.monotonic() >= absolute_deadline: result["error"] = "probe timeout"; return result
        proc = subprocess.Popen([str(path), "app-server", "-c", 'service_tier="fast"'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.stdin is None or proc.stdout is None: raise RuntimeError("stdio unavailable")
        q: queue.Queue[str | None] = queue.Queue()
        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout: q.put(line)
            q.put(None)
        threading.Thread(target=pump, daemon=True).start()
        def call(payload: dict[str, Any], request_id: int | None) -> dict[str, Any]:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n"); proc.stdin.flush()
            while time.monotonic() < absolute_deadline:
                try: line = q.get(timeout=min(0.2, max(0.01, absolute_deadline - time.monotonic())))
                except queue.Empty: continue
                if line is None: raise RuntimeError("app-server exited")
                try: obj = json.loads(line)
                except json.JSONDecodeError: continue
                if request_id is None or obj.get("id") == request_id: return obj
            raise TimeoutError("probe timeout")
        init = call({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-usage-guard", "title": f"{APP_NAME} v{APP_VERSION}", "version": APP_VERSION}}}, 1)
        if "error" in init: raise RuntimeError(redact(AppServerProvider._error_text(init["error"])))
        result["initialize"] = True
        # initialized is a notification: send-only, never wait for a response.
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"method": "initialized"}, separators=(",", ":")) + "\n"); proc.stdin.flush()
        limits = call({"id": 2, "method": "account/rateLimits/read"}, 2)
        if "error" in limits: raise RuntimeError(redact(AppServerProvider._error_text(limits["error"])))
        snapshot_from_result(limits.get("result"), source="app-server"); result["rateLimits"] = True; result["ok"] = True
    except Exception as exc:
        result["error"] = redact(str(exc))
    finally:
        proc_obj = locals().get("proc")
        if proc_obj is not None:
            try:
                if proc_obj.stdin: proc_obj.stdin.close()
            except OSError: pass
            try: proc_obj.wait(timeout=0.3)
            except Exception:
                try: proc_obj.terminate(); proc_obj.wait(timeout=0.5)
                except Exception:
                    try: proc_obj.kill(); proc_obj.wait(timeout=0.5)
                    except Exception: pass
    return result


class AppServerProvider:
    def __init__(self, *, poll_seconds: float = POLL_SECONDS, process_factory=subprocess.Popen) -> None:
        self.poll_seconds = poll_seconds
        self.process_factory = process_factory
        self.lock = threading.RLock(); self.ensure_lock = threading.Lock(); self.io_lock = threading.Lock(); self.stop_event = threading.Event()
        self.process: subprocess.Popen[str] | None = None; self.stdin = None; self.messages: queue.Queue[str | None] = queue.Queue()
        self.reader: threading.Thread | None = None; self.worker: threading.Thread | None = None
        self.next_id = 1; self.pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.snapshot: Snapshot | None = None; self.activity: dict[str, Any] | None = None; self.last_error = ""; self.snapshot_generation = 0
        self.refresh_state_lock = threading.Lock(); self.refresh_cycle: RefreshCycle | None = None; self.refresh_needed = False
        self.selected_candidate: str | None = None; self.selected_version: str | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)

    def start(self) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive(): return
            self.stop_event.clear(); self.worker = threading.Thread(target=self._poll_loop, daemon=True, name="codex-usage-guard-poll"); self.worker.start()

    def _poll_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.read(force=True, include_usage=False)
            except Exception as exc: self.last_error = redact(str(exc))
            self.stop_event.wait(self.poll_seconds)

    def _ensure(self, deadline: float | None = None) -> None:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining is not None and not self.ensure_lock.acquire(timeout=remaining): raise TimeoutError("Codex App Server startup deadline exceeded")
        self.ensure_lock.acquire() if remaining is None else None
        try:
            if self.process is not None and self.process.poll() is None: return
            path, diagnostics = find_codex_executable(deadline=deadline)
            if path is None: raise RuntimeError("Codex CLI unavailable; " + "; ".join(diagnostics[-3:]))
            candidates = [path] + [item for item in find_candidates() if item != path]
            last_error: Exception | None = None
            for candidate in candidates:
                try:
                    if deadline is not None and time.monotonic() >= deadline: raise TimeoutError("Codex App Server candidate deadline exceeded")
                    messages = queue.Queue(); self.messages = messages
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    process = self.process_factory([str(candidate), "app-server", "-c", 'service_tier="fast"'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
                    self.process = process; self.stdin = process.stdin
                    if process.stdout is None or process.stderr is None or process.stdin is None: raise RuntimeError("Codex App Server stdio unavailable")
                    self.reader = threading.Thread(target=self._read_loop, args=(process.stdout, messages), daemon=True); self.reader.start()
                    threading.Thread(target=self._stderr_loop, args=(process.stderr,), daemon=True).start()
                    self._send_raw({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-usage-guard", "title": f"{APP_NAME} v{APP_VERSION}", "version": APP_VERSION}}})
                    response = self._wait_response(1, deadline=deadline)
                    if "error" in response: raise RuntimeError(self._error_text(response["error"]))
                    self.next_id = max(self.next_id, 2); self._send_raw({"method": "initialized"}); probe = self._request("account/rateLimits/read", deadline=deadline); snapshot_from_result(probe.get("result"), source="app-server"); self.selected_candidate = str(candidate)
                    try:
                        remaining = 5.0 if deadline is None else max(0.01, deadline - time.monotonic()); version_probe = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=min(5.0, remaining), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)); self.selected_version = ".".join(str(x) for x in _version((version_probe.stdout or "") + (version_probe.stderr or "")))
                    except Exception: self.selected_version = None
                    self.stop_event.clear()
                    return
                except Exception as exc:
                    last_error = exc; self.stop(deadline=deadline); self.stop_event.clear()
            raise RuntimeError("No Codex App Server candidate completed handshake: " + redact(str(last_error)))
        finally:
            self.ensure_lock.release()

    def _read_loop(self, stream: Any, messages: queue.Queue[str | None]) -> None:
        for line in stream:
            raw = line.rstrip("\r\n")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if self.messages is not messages:
                return
            if isinstance(payload, dict) and payload.get("id") == 1:
                messages.put(raw)
            elif isinstance(payload, dict):
                self._handle_notification(payload)
        # EOF/crash must wake all outstanding callers immediately.  The reader
        # is process-generation scoped by its private queue; stale responses
        # therefore cannot satisfy a new candidate's waiters.
        with self.lock:
            if self.messages is not messages:
                return
            for event, result in self.pending.values():
                result["error"] = {"message": "Codex App Server process exited"}
                event.set()
            self.pending.clear()

    def _stderr_loop(self, stream: Any) -> None:
        for line in stream:
            value = redact(line)
            if value: self.stderr_tail.append(value)

    def _send_raw(self, payload: MappingLike) -> None:
        if self.stdin is None: raise RuntimeError("App Server stdin unavailable")
        with self.io_lock:
            self.stdin.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"); self.stdin.flush()

    def _wait_response(self, request_id: int, timeout: float | None = None, *, deadline: float | None = None) -> dict[str, Any]:
        absolute = deadline if deadline is not None else time.monotonic() + (15.0 if timeout is None else timeout)
        while time.monotonic() < absolute:
            try: line = self.messages.get(timeout=min(0.2, max(0.01, absolute - time.monotonic())))
            except queue.Empty: continue
            if not line: continue
            try: payload = json.loads(line)
            except json.JSONDecodeError: continue
            if isinstance(payload, dict) and payload.get("id") == request_id: return payload
            self._handle_notification(payload)
        raise TimeoutError("Codex App Server response timed out")

    def _request(self, method: str, timeout: float | None = None, *, deadline: float | None = None) -> dict[str, Any]:
        absolute = deadline if deadline is not None else time.monotonic() + (15.0 if timeout is None else timeout)
        if time.monotonic() >= absolute: raise TimeoutError(f"{method} deadline exceeded")
        with self.lock:
            request_id = self.next_id; self.next_id += 1; event = threading.Event(); result: dict[str, Any] = {}; self.pending[request_id] = (event, result)
            self._send_raw({"id": request_id, "method": method})
        if not event.wait(max(0.0, absolute - time.monotonic())):
            with self.lock: self.pending.pop(request_id, None)
            raise TimeoutError(f"{method} timed out")
        if "error" in result: raise RuntimeError(self._error_text(result["error"]))
        return result

    def _handle_notification(self, payload: Any) -> None:
        if not isinstance(payload, dict): return
        with self.lock:
            request_id = payload.get("id")
            if isinstance(request_id, int) and request_id in self.pending:
                event, result = self.pending.pop(request_id); result.update(payload); event.set(); return
            if payload.get("method") == "account/rateLimits/updated" and isinstance(payload.get("params"), dict):
                try:
                    self.snapshot = merge_notification(payload["params"], self.snapshot)
                    raw_limits = payload["params"].get("rateLimits", {})
                    if isinstance(raw_limits, dict) and str(raw_limits.get("limitId")) == "codex":
                        meaningful_window = any(isinstance(raw_limits.get(key), dict) and "usedPercent" in raw_limits.get(key, {}) for key in ("primary", "secondary"))
                        if meaningful_window or ("rateLimitReachedType" in raw_limits and raw_limits.get("rateLimitReachedType") is not None): self.snapshot_generation += 1
                        elif "rateLimitReachedType" in raw_limits and raw_limits.get("rateLimitReachedType") is None: self.refresh_needed = True

                except UsagePayloadError:
                    # An unrelated or malformed sparse notification cannot be
                    # used to refresh the selected Codex bucket.  Schedule an
                    # explicit full read on the next refresh attempt.
                    self.refresh_needed = True

    def read(self, *, force: bool = False, include_usage: bool = True, deadline: float | None = None) -> Snapshot:
        absolute = deadline if deadline is not None else time.monotonic() + 15.0
        self.start(); self._ensure(absolute)
        with self.refresh_state_lock:
            if self.refresh_cycle is not None:
                cycle = self.refresh_cycle
                leader = False
            elif not force and not self.refresh_needed and self.snapshot and time.time() - self.snapshot.fetched_at < STALE_SECONDS:
                return self.snapshot
            else:
                cycle = RefreshCycle(include_activity=include_usage); self.refresh_cycle = cycle; leader = True
        if not leader:
            if not cycle.event.wait(max(0.0, absolute - time.monotonic())): raise TimeoutError("refresh timed out")
            if cycle.error is not None: raise cycle.error
            if include_usage and not cycle.include_activity:
                try: self.activity = parse_usage_activity(self._request("account/usage/read", deadline=absolute).get("result"))
                except Exception: self.activity = None
            if cycle.result is None: raise RuntimeError("refresh produced no snapshot")
            return cycle.result
        try:
            generation = self.snapshot_generation
            # Consume a pre-existing refresh request; notifications arriving
            # during this cycle set it again and are preserved for the next one.
            self.refresh_needed = False
            response = self._request("account/rateLimits/read", deadline=absolute)
            result = response.get("result")
            # A codex notification received while the request was in flight is
            # newer than the response; retain it instead of regressing state.
            if self.snapshot_generation == generation or self.snapshot is None:
                self.snapshot = snapshot_from_result(result, source="app-server")
            if include_usage:
                try: self.activity = parse_usage_activity(self._request("account/usage/read", deadline=absolute).get("result")); cycle.include_activity = True
                except Exception: self.activity = None
            result_snapshot = self.snapshot
            if result_snapshot is None: raise RuntimeError("rate-limit response produced no snapshot")
            cycle.result = result_snapshot
            return result_snapshot
        except Exception as exc:
            cycle.error = exc
            raise
        finally:
            with self.refresh_state_lock:
                if self.refresh_cycle is cycle: self.refresh_cycle = None
                cycle.event.set()

    def status(self, *, force: bool = False, include_usage: bool = True, timeout_sec: int = 15) -> dict[str, Any]:
        try:
            absolute = time.monotonic() + float(timeout_sec)
            snapshot = self.read(force=force, include_usage=include_usage, deadline=absolute)
            data = snapshot.to_dict(); data["usage"] = self.activity if include_usage else None; data["ageSec"] = time.time() - snapshot.fetched_at; data["status"] = "live" if 0 <= data["ageSec"] < STALE_SECONDS else "stale"; data["freshness"] = {"fresh": data["status"] == "live", "ageSec": data["ageSec"]}; data["effectiveRemainingPercent"] = snapshot.min_remaining; data["selectedCodex"] = next((b.to_dict() for b in snapshot.buckets if b.limit_id == "codex"), None); data["diagnostics"] = []
            from policy import evaluate
            save_cache(snapshot, str(evaluate(snapshot)["decision"]))
            return data
        except Exception as exc:
            self.last_error = redact(str(exc)); cached = load_fallback_cache()
            if cached is not None and 0 <= time.time() - cached.fetched_at < STALE_SECONDS:
                data = cached.to_dict(); data["status"] = "cached"; data["ageSec"] = max(0.0, time.time() - cached.fetched_at); data["freshness"] = {"fresh": data["ageSec"] < STALE_SECONDS, "ageSec": data["ageSec"]}; data["effectiveRemainingPercent"] = cached.min_remaining; data["selectedCodex"] = next((b.to_dict() for b in cached.buckets if b.limit_id == "codex"), None); data["diagnostics"] = [self.last_error]; return data
            lowered = self.last_error.lower()
            state = "unauthenticated" if any(word in lowered for word in ("login", "auth", "unauthor", "sign in")) else ("schema_unknown" if "schema" in lowered or "usable rate" in lowered else "unavailable")
            return {"schemaVersion": 1, "status": state, "source": "none", "ageSec": None, "freshness": {"fresh": False, "ageSec": None}, "buckets": [], "usage": None, "effectiveRemainingPercent": None, "selectedCodex": None, "diagnostics": [self.last_error]}

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict) and error.get("message"): return str(error["message"])
        return str(error)

    def stop(self, *, deadline: float | None = None) -> None:
        self.stop_event.set(); process = self.process; self.process = None; self.stdin = None; self.selected_candidate = None; self.selected_version = None
        with self.lock:
            for event, result in self.pending.values(): result["error"] = {"message": "provider stopped"}; event.set()
            self.pending.clear()
        if process is None: return
        try:
            if process.stdin: process.stdin.close()
        except OSError: pass
        wait_for = 0.0 if deadline is not None and time.monotonic() >= deadline else (0.5 if deadline is None else min(0.5, max(0.0, deadline - time.monotonic())))
        try: process.wait(timeout=wait_for)
        except (OSError, subprocess.TimeoutExpired):
            try: process.terminate(); process.wait(timeout=0.0 if deadline is not None and time.monotonic() >= deadline else (1.0 if deadline is None else max(0.0, deadline - time.monotonic())))
            except (OSError, subprocess.TimeoutExpired):
                try: process.kill(); process.wait(timeout=0.0 if deadline is not None and time.monotonic() >= deadline else 1.0)
                except (OSError, subprocess.TimeoutExpired): pass


MappingLike = dict[str, Any]
_provider = AppServerProvider()
atexit.register(_provider.stop)


def cache_path() -> Path:
    root = os.environ.get("CODEX_USAGE_GUARD_DATA_DIR") or str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OpenAI" / "codex-usage-guard")
    return Path(root) / "state.json"


def save_cache(snapshot: Snapshot, decision: str = "unknown") -> None:
    path = cache_path(); path.parent.mkdir(parents=True, exist_ok=True); handle, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=path.parent); temporary = Path(name)
    payload = {"schemaVersion": 1, "fetchedAt": snapshot.fetched_at, "source": snapshot.source, "buckets": [b.to_dict() for b in snapshot.buckets], "minRemainingPercent": snapshot.min_remaining, "decision": decision}
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_fallback_cache() -> Snapshot | None:
    paths = [cache_path(), Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Codex Usage Indicator" / "state.json"]
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")); snapshot = snapshot_from_cache(payload)
            age = time.time() - snapshot.fetched_at
            if age < 0 or age >= STALE_SECONDS: continue
            if "Indicator" in str(path): snapshot = Snapshot(snapshot.buckets, snapshot.fetched_at, "usage-indicator-cache", snapshot.usage)
            elif path == cache_path(): snapshot = Snapshot(snapshot.buckets, snapshot.fetched_at, "codex-usage-guard-cache", snapshot.usage)
            return snapshot
        except (OSError, json.JSONDecodeError, UsagePayloadError, TypeError, ValueError): pass
    return None
