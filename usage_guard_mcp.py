from __future__ import annotations

import json
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from app_server import APP_NAME, APP_VERSION, _provider, save_cache
from policy import evaluate

mcp = FastMCP("codex-usage-guard", instructions="Codex Usage Guard v0.2.0 provides advisory rate-limit checkpoints.")


def _json(value: dict[str, Any]) -> str:
    value.setdefault("schemaVersion", 1); value.setdefault("product", f"{APP_NAME} v{APP_VERSION}"); value.setdefault("version", APP_VERSION)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _valid_timeout(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 60


@mcp.tool()
def get_usage_status(force_refresh: bool = True, include_activity: bool = False, timeout_sec: int = 15) -> str:
    """Return sanitized rate-limit windows and optional account activity summaries."""
    if not _valid_timeout(timeout_sec): return _json({"status": "error", "error": "timeout_sec must be an integer from 1 to 60"})
    return _json(_provider.status(force=force_refresh, include_usage=include_activity, timeout_sec=timeout_sec))


@mcp.tool()
def evaluate_usage_guard(purpose: Literal["task_start", "before_delegate", "before_build", "before_external_cli", "checkpoint", "manual"] = "checkpoint", force_refresh: bool = True, timeout_sec: int = 15) -> str:
    """Apply the fixed fail-closed policy to the selected Codex bucket."""
    if purpose not in {"task_start", "before_delegate", "before_build", "before_external_cli", "checkpoint", "manual"}: return _json({"status": "error", "error": "unsupported purpose"})
    if not _valid_timeout(timeout_sec): return _json({"status": "error", "error": "timeout_sec must be an integer from 1 to 60"})
    status = _provider.status(force=force_refresh, include_usage=False, timeout_sec=timeout_sec)
    snapshot = _provider.snapshot if status.get("status") == "live" else None
    if snapshot is None and status.get("status") == "cached":
        from models import snapshot_from_cache
        try: snapshot = snapshot_from_cache(status)
        except Exception: snapshot = None
    result = evaluate(snapshot, status=status.get("status", "unknown"), purpose=purpose)
    result["source"] = status.get("source"); result["diagnostics"] = status.get("diagnostics", [])
    if snapshot is not None: save_cache(snapshot, str(result.get("decision", "unknown")))
    return _json(result)


@mcp.tool()
def doctor(timeout_sec: int = 15) -> str:
    """Report resolver and runtime health without reading credentials or config.toml."""
    if not _valid_timeout(timeout_sec): return _json({"status": "error", "error": "timeout_sec must be an integer from 1 to 60"})
    from app_server import find_codex_executable, find_candidates, probe_candidate
    doctor_deadline = time.monotonic() + float(timeout_sec)
    probe_status = _provider.status(force=True, include_usage=False, timeout_sec=max(1, min(timeout_sec, int(max(1, doctor_deadline - time.monotonic())))))
    if _provider.selected_candidate:
        path, diagnostics = __import__('pathlib').Path(_provider.selected_candidate), []
    else:
        path, diagnostics = find_codex_executable(deadline=doctor_deadline)
    live = probe_status.get("status") == "live" and _provider.snapshot is not None and _provider.process is not None and _provider.process.poll() is None
    data_dir = __import__('app_server').cache_path().parent; heartbeat = data_dir / "heartbeat.txt"
    heartbeat_age = None
    try: heartbeat_age = time.time() - heartbeat.stat().st_mtime
    except OSError: pass
    cache_file = data_dir / "state.json"; cache_source = "none"; cache_age = None; cache_fresh = False
    try:
        from models import snapshot_from_cache
        cache_snapshot = snapshot_from_cache(json.loads(cache_file.read_text(encoding="utf-8"))); cache_source = cache_snapshot.source; cache_age = time.time() - cache_snapshot.fetched_at; cache_fresh = 0 <= cache_age < 120
    except Exception:
        pass
    candidates = find_candidates(); candidate_probes = []
    for candidate in candidates:
        if time.monotonic() >= doctor_deadline: break
        if _provider.selected_candidate and str(candidate).casefold() == str(_provider.selected_candidate).casefold():
            candidate_probes.append({"candidate": str(candidate), "version": _provider.selected_version, "initialize": True, "rateLimits": bool(_provider.snapshot), "ok": live, "reused": True})
        else:
            candidate_probes.append(probe_candidate(candidate, timeout=max(0.01, doctor_deadline - time.monotonic()), deadline=doctor_deadline))
    degraded = [] if live else list(probe_status.get("diagnostics") or [])
    if heartbeat_age is None or heartbeat_age >= 900: degraded.append("hook heartbeat missing or stale")
    if len(candidate_probes) < len(candidates): degraded.append("candidate diagnostics deadline exceeded")
    if any(not bool(item.get("ok")) for item in candidate_probes): degraded.append("candidate handshake degraded")
    return _json({"status": "ok" if live and not degraded else "degraded", "resolver": {"selected": str(path) if path else None, "candidateCount": len(candidates), "diagnostics": diagnostics, "candidates": candidate_probes}, "providerHandshake": {"status": "live" if live else probe_status.get("status"), "selectedCandidate": _provider.selected_candidate, "selectedVersion": _provider.selected_version, "rateLimitsProbe": bool(_provider.snapshot)}, "cache": {"path": str(cache_file), "source": cache_source, "fresh": cache_fresh, "ageSec": cache_age}, "hook": {"heartbeat": "active" if heartbeat_age is not None and heartbeat_age < 900 else "missing", "heartbeatAgeSec": heartbeat_age, "trust": "unverified", "degradedReason": degraded or None}, "rawCredentialAccess": False, "watching": bool(_provider.worker and _provider.worker.is_alive()), "dataDir": str(data_dir)})


if __name__ == "__main__":
    mcp.run()
