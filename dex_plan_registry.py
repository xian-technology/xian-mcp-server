"""Process-local, single-use registry for server-issued DEX plans."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable


class PlanRegistryError(ValueError):
    """Raised when a plan cannot be issued or claimed safely."""


@dataclass(frozen=True)
class ClaimedPlan:
    """A plan atomically removed from the registry for one submission attempt."""

    plan_id: str
    plan_digest: str
    issued_at: str
    expires_at: str
    plan: dict[str, Any]


@dataclass(frozen=True)
class _StoredPlan:
    canonical_json: bytes
    plan_digest: str
    issued_at: datetime
    expires_at: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PlanRegistryError("Plan timestamps must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PlanRegistryError(f"{field} must be an ISO-8601 timestamp")
    try:
        return _as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise PlanRegistryError(f"{field} must be an ISO-8601 timestamp") from exc


def _canonical_json(plan: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            plan,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PlanRegistryError(f"Plan is not canonical JSON: {exc}") from exc


class PlanRegistry:
    """Bounded, thread-safe storage for immutable, expiring, single-use plans.

    Plans are stored as canonical JSON bytes rather than caller-mutable dictionaries.
    Claiming removes a plan under the lock before any wallet or network work begins,
    so concurrent calls and retries after a partial multi-call submission cannot replay
    it. Registry contents deliberately disappear when the server process restarts.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_entries: int = 256,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("ttl_seconds must be between 30 and 900")
        if not 1 <= max_entries <= 1_000:
            raise ValueError("max_entries must be between 1 and 1000")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._plans: OrderedDict[str, _StoredPlan] = OrderedDict()
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def _cleanup_locked(self, now: datetime) -> None:
        expired = [plan_id for plan_id, stored in self._plans.items() if stored.expires_at <= now]
        for plan_id in expired:
            self._plans.pop(plan_id, None)

    def issue(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Register a canonical copy and return its complete audit representation."""
        if not isinstance(plan, dict):
            raise PlanRegistryError("Plan must be an object")
        reserved = {"plan_id", "plan_digest", "issued_at", "expires_at"}
        conflict = reserved.intersection(plan)
        if conflict:
            raise PlanRegistryError(
                f"Plan contains reserved registry fields: {', '.join(sorted(conflict))}"
            )

        canonical = _canonical_json(plan)
        digest = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        now = self._now()
        deadline = _parse_timestamp(plan.get("deadline", {}).get("iso"), field="Plan deadline")
        expires_at = min(deadline, now + timedelta(seconds=self.ttl_seconds))
        if expires_at <= now:
            raise PlanRegistryError("Plan deadline has already expired")

        with self._lock:
            self._cleanup_locked(now)
            while len(self._plans) >= self.max_entries:
                self._plans.popitem(last=False)
            plan_id = secrets.token_urlsafe(32)
            while plan_id in self._plans:
                plan_id = secrets.token_urlsafe(32)
            self._plans[plan_id] = _StoredPlan(
                canonical_json=canonical,
                plan_digest=digest,
                issued_at=now,
                expires_at=expires_at,
            )

        audit = json.loads(canonical)
        audit.update(
            {
                "plan_id": plan_id,
                "plan_digest": digest,
                "issued_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
        )
        return audit

    def claim(self, plan_id: str) -> ClaimedPlan:
        """Atomically consume a plan before starting a submission attempt."""
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PlanRegistryError("plan_id is required")
        now = self._now()
        with self._lock:
            self._cleanup_locked(now)
            stored = self._plans.pop(plan_id.strip(), None)
        if stored is None:
            raise PlanRegistryError(
                "Unknown, expired, evicted, already consumed, or invalidated plan_id; "
                "create a fresh plan"
            )
        if stored.expires_at <= now:
            raise PlanRegistryError("Plan has expired; create a fresh plan")
        actual_digest = f"sha256:{hashlib.sha256(stored.canonical_json).hexdigest()}"
        if not secrets.compare_digest(actual_digest, stored.plan_digest):
            raise PlanRegistryError("Stored plan integrity check failed")
        return ClaimedPlan(
            plan_id=plan_id.strip(),
            plan_digest=stored.plan_digest,
            issued_at=stored.issued_at.isoformat(),
            expires_at=stored.expires_at.isoformat(),
            plan=json.loads(stored.canonical_json),
        )

    def __len__(self) -> int:
        now = self._now()
        with self._lock:
            self._cleanup_locked(now)
            return len(self._plans)
