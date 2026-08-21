from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Any, Mapping


class UsagePayloadError(ValueError):
    pass


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsagePayloadError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise UsagePayloadError(f"{label} must be finite")
    return number


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    resets_at: float | None = None
    window_duration_mins: float | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "RateLimitWindow | None":
        if payload is None:
            return None
        if not isinstance(payload, Mapping) or "usedPercent" not in payload:
            raise UsagePayloadError("window requires usedPercent")
        used = max(0.0, min(100.0, _finite_number(payload["usedPercent"], "usedPercent")))
        reset = payload.get("resetsAt")
        duration = payload.get("windowDurationMins")
        return cls(
            used,
            None if reset is None else _finite_number(reset, "resetsAt"),
            None if duration is None else _finite_number(duration, "windowDurationMins"),
        )

    def to_dict(self) -> dict[str, float | None]:
        return {"usedPercent": self.used_percent, "resetsAt": self.resets_at, "windowDurationMins": self.window_duration_mins}


@dataclass(frozen=True)
class Credits:
    has_credits: bool
    unlimited: bool
    balance: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "Credits | None":
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise UsagePayloadError("credits must be an object")
        return cls(bool(payload.get("hasCredits", False)), bool(payload.get("unlimited", False)), None if payload.get("balance") is None else str(payload["balance"]))

    def to_dict(self) -> dict[str, Any]:
        return {"hasCredits": self.has_credits, "unlimited": self.unlimited, "balance": self.balance}


@dataclass(frozen=True)
class Bucket:
    limit_id: str
    limit_name: str | None
    plan_type: str | None
    primary: RateLimitWindow | None
    secondary: RateLimitWindow | None
    credits: Credits | None
    reached_type: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], fallback_id: str = "codex") -> "Bucket":
        if not isinstance(payload, Mapping):
            raise UsagePayloadError("bucket must be an object")
        return cls(
            str(payload.get("limitId") or fallback_id),
            None if payload.get("limitName") is None else str(payload["limitName"]),
            None if payload.get("planType") is None else str(payload["planType"]),
            RateLimitWindow.from_payload(payload.get("primary")),
            RateLimitWindow.from_payload(payload.get("secondary")),
            Credits.from_payload(payload.get("credits")),
            None if payload.get("rateLimitReachedType") is None else str(payload["rateLimitReachedType"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"limitId": self.limit_id, "limitName": self.limit_name, "planType": self.plan_type,
                "primary": self.primary.to_dict() if self.primary else None,
                "secondary": self.secondary.to_dict() if self.secondary else None,
                "credits": self.credits.to_dict() if self.credits else None,
                "rateLimitReachedType": self.reached_type}


@dataclass(frozen=True)
class Snapshot:
    buckets: tuple[Bucket, ...]
    fetched_at: float
    source: str = "app-server"
    usage: dict[str, Any] | None = None

    @property
    def min_remaining(self) -> float | None:
        selected = next((b for b in self.buckets if b.limit_id == "codex"), None)
        if selected is None:
            return None
        windows = [w for w in (selected.primary, selected.secondary) if w is not None]
        return min((w.remaining_percent for w in windows), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {"schemaVersion": 1, "fetchedAt": self.fetched_at, "source": self.source,
                "buckets": [b.to_dict() for b in self.buckets], "usage": self.usage}


def snapshot_from_result(result: Mapping[str, Any], *, source: str = "app-server", fetched_at: float | None = None) -> Snapshot:
    if not isinstance(result, Mapping):
        raise UsagePayloadError("rate-limit result must be an object")
    buckets: list[Bucket] = []
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, Mapping):
        for key, raw in by_id.items():
            if isinstance(raw, Mapping):
                try:
                    item = dict(raw); item["limitId"] = str(key); buckets.append(Bucket.from_payload(item, str(key)))
                except UsagePayloadError:
                    continue
    base = result.get("rateLimits")
    if isinstance(base, Mapping):
        try:
            item = Bucket.from_payload(base)
            if not any(x.limit_id == item.limit_id for x in buckets): buckets.append(item)
        except UsagePayloadError:
            pass
    if not buckets:
        raise UsagePayloadError("no usable rate-limit buckets")
    buckets.sort(key=lambda b: (b.limit_id != "codex", b.limit_id.casefold()))
    return Snapshot(tuple(buckets), time.time() if fetched_at is None else fetched_at, source)


def merge_notification(params: Mapping[str, Any], previous: Snapshot | None) -> Snapshot:
    raw = params.get("rateLimits") if isinstance(params, Mapping) else None
    if not isinstance(raw, Mapping) or not raw.get("limitId"):
        raise UsagePayloadError("notification has no limitId")
    limit_id = str(raw["limitId"])
    existing = next((b for b in previous.buckets if b.limit_id == limit_id), None) if previous else None
    if existing is None:
        updated = Bucket.from_payload(raw)
    else:
        merged = existing.to_dict()
        for key in ("limitName", "rateLimitReachedType"):
            if key in raw: merged[key] = raw[key]
        # Nullable fields are explicit clears.  Omitted fields are preserved so
        # sparse notifications cannot erase an existing window/metadata value.
        for key in ("planType", "credits"):
            if key in raw: merged[key] = raw[key]
        for key in ("primary", "secondary"):
            if key in raw:
                incoming = raw[key]; old = merged.get(key)
                if isinstance(incoming, Mapping) and isinstance(old, Mapping):
                    combined = dict(old); combined.update(incoming); merged[key] = combined
                else: merged[key] = incoming
        updated = Bucket.from_payload(merged)
    buckets = [b for b in (previous.buckets if previous else ()) if b.limit_id != limit_id] + [updated]
    buckets.sort(key=lambda b: (b.limit_id != "codex", b.limit_id.casefold()))
    meaningful = any(key in raw and isinstance(raw.get(key), Mapping) and "usedPercent" in raw.get(key, {}) for key in ("primary", "secondary"))
    authoritative_reached = "rateLimitReachedType" in raw and raw.get("rateLimitReachedType") is not None
    meaningful = meaningful or authoritative_reached
    timestamp = time.time() if previous is None or (limit_id == "codex" and meaningful) else previous.fetched_at
    return Snapshot(tuple(buckets), timestamp, previous.source if previous else "app-server", previous.usage if previous else None)


def parse_usage_activity(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, Mapping): return None
    budget = [0]
    sensitive_exact = {"accountid", "chatgptaccountid", "userid", "workspaceid", "organizationid", "email", "token", "auth", "secret", "credential", "apikey", "accesstoken", "refreshtoken", "authorization"}
    def safe(value: Any, key: str = "", depth: int = 0) -> Any:
        if budget[0] >= 1024: return None
        budget[0] += 1
        normalized = "".join(ch for ch in key.lower() if ch.isalnum())
        if depth > 0 and (normalized in sensitive_exact or normalized.endswith("token") or normalized.endswith("accountid") or normalized.endswith("userid")): return None
        if isinstance(value, bool) or value is None: return None
        if isinstance(value, (int, float)) and math.isfinite(float(value)): return value
        if isinstance(value, str) and key.lower() in {"date", "day", "period", "model", "startdate", "enddate"} and len(value) <= 32: return value
        if isinstance(value, list):
            items = []
            for item in value:
                if budget[0] >= 1024 or len(items) >= 256: break
                parsed = safe(item, key, depth + 1)
                if parsed is not None: items.append(parsed)
            return items
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for k, raw in value.items():
                if budget[0] >= 1024: break
                name = str(k)
                if len(name) > 32: continue
                parsed = safe(raw, name, depth + 1)
                if parsed is not None: output[name] = parsed
            return output
        return None
    # Account activity is intentionally allowlisted and never enters policy.
    out: dict[str, Any] = {}
    for key in ("daily", "days", "summaries", "summary", "dailyUsageBuckets", "tokenActivity", "totalTokens"):
        value = safe(result.get(key), key)
        if value is not None: out[key] = value
    return out or None


def cache_payload(snapshot: Snapshot, decision: str, min_remaining: float | None) -> dict[str, Any]:
    return {"schemaVersion": 1, "fetchedAt": snapshot.fetched_at, "source": snapshot.source,
            "buckets": [b.to_dict() for b in snapshot.buckets], "minRemainingPercent": min_remaining, "decision": decision}


def snapshot_from_cache(payload: Mapping[str, Any]) -> Snapshot:
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("buckets"), list): raise UsagePayloadError("unsupported cache")
    buckets = tuple(Bucket.from_payload(x) for x in payload["buckets"] if isinstance(x, Mapping))
    if not buckets: raise UsagePayloadError("cache has no buckets")
    fetched = _finite_number(payload.get("fetchedAt"), "fetchedAt")
    if fetched > time.time(): raise UsagePayloadError("cache timestamp is in the future")
    return Snapshot(buckets, fetched, str(payload.get("source") or "cache"))
