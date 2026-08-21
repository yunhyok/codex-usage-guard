from __future__ import annotations

import time
from typing import Any, Mapping

from models import Snapshot

STALE_SECONDS = 120


def evaluate(snapshot: Snapshot | None, *, now: float | None = None, status: str = "live", purpose: str = "checkpoint") -> dict[str, Any]:
    now = time.time() if now is None else now
    age = None if snapshot is None else now - snapshot.fetched_at
    remaining = None if snapshot is None else snapshot.min_remaining
    reasons: list[str] = []
    selected = next((b for b in snapshot.buckets if b.limit_id == "codex"), None) if snapshot else None
    reached = bool(selected and selected.reached_type)
    if reached and age is not None and 0 <= age < STALE_SECONDS:
        return {"schemaVersion": 1, "decision": "critical", "purpose": purpose, "status": status, "ageSec": age, "freshness": {"fresh": True, "ageSec": age}, "minRemainingPercent": remaining, "blockingReasons": ["selected Codex rate limit reached"], "recommendedAction": "save state and ask the user before continuing", "allowDelegation": False, "allowExpensivePhase": False, "mustCheckpoint": True, "advisory": True, "currentTaskMayContinue": True, "cannotForceStop": True}
    if snapshot is None or remaining is None or age is None or age < 0 or age >= STALE_SECONDS:
        return {"schemaVersion": 1, "decision": "unknown", "purpose": purpose, "status": status,
                "ageSec": age, "freshness": {"fresh": False, "ageSec": age}, "minRemainingPercent": remaining, "blockingReasons": ["no fresh valid rate-limit window"],
                "recommendedAction": "checkpoint and refresh usage before costly work", "allowDelegation": False, "allowExpensivePhase": False, "mustCheckpoint": True, "advisory": True, "currentTaskMayContinue": True,
                "cannotForceStop": True}
    if reached or remaining <= 10:
        decision = "critical"; reasons.append("selected Codex rate limit reached" if reached else "remaining usage is 10% or less")
    elif remaining <= 25:
        decision = "checkpoint"; reasons.append("remaining usage is above 10% and at most 25%")
    elif remaining <= 40:
        decision = "watch"; reasons.append("remaining usage is above 25% and at most 40%")
    else:
        decision = "proceed"
        soon = [w.resets_at for b in snapshot.buckets if b.limit_id == "codex" for w in (b.primary, b.secondary) if w and w.resets_at and 0 <= w.resets_at - now < 1800]
        if soon: reasons.append("a selected window resets within 30 minutes")
    action = {"proceed": "continue", "watch": "keep work bounded and checkpoint soon", "checkpoint": "save state; do not start expensive new work", "critical": "save state and ask the user before continuing"}[decision]
    return {"schemaVersion": 1, "decision": decision, "purpose": purpose, "status": status, "ageSec": age, "freshness": {"fresh": True, "ageSec": age},
            "minRemainingPercent": remaining, "blockingReasons": reasons, "recommendedAction": action,
            "allowDelegation": decision == "proceed", "allowExpensivePhase": decision == "proceed", "mustCheckpoint": decision in {"checkpoint", "critical", "unknown"}, "advisory": True, "currentTaskMayContinue": True, "cannotForceStop": True}
