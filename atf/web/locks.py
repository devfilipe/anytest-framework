"""Per-resource run locks. A run (test plan or ad-hoc) locks the bench resources it touches —
the target boards + the agents their drivers/actions use. Another run needing ANY held resource is
rejected immediately. In-memory, thread-safe; forced-unlock is the emergency escape.

A **reservation** holds resources across several runs: a user takes it with a time to live, runs
naming it reuse its resources instead of finding them busy, and it ends when released or when its
time runs out — never while a run under it is still going (it then ends with that run)."""
from __future__ import annotations

import secrets
import threading
import time

MAX_TTL = 24 * 3600                    # a reservation lasts at most a day; take another to go on


class ReservationError(Exception):
    """A run named a reservation it cannot use (unknown, expired, released or someone else's)."""


def touched_resources(bench, board_names=None) -> set:
    """Resources a run over `board_names` (or all) touches: `board:<name>` for each target board
    + `agent:<name>` for each agent its console/craft drivers and actions use. mgmt needs no agent."""
    res: set = set()
    for b in bench.boards:
        if board_names and b.name not in board_names:
            continue
        res.add(f"board:{b.name}")
        for drv in ("console", "craft"):
            a = (b.drivers.get(drv) or {}).get("agent")
            if a:
                res.add(f"agent:{a}")
        for _an, a in (b.actions or {}).items():
            if a.get("agent"):
                res.add(f"agent:{a['agent']}")
    return res


class ResourceLocks:
    def __init__(self, clock=time.time):
        self._held: dict = {}          # resource -> {"run","who","since"}; "run" is a reservation id when reserved
        self._resv: dict = {}          # reservation id -> {"owner","who","resources","since","expires","runs","ended"}
        self._lock = threading.Lock()
        self._now = clock

    # --- caller holds _lock ---
    def _over(self, r: dict) -> bool:
        return r["ended"] or r["expires"] <= self._now()

    def _expire(self) -> None:
        """Drop the reservations that are over (time ran out, or released) and that no run is using."""
        for rid in [rid for rid, r in self._resv.items() if self._over(r) and not r["runs"]]:
            self._resv.pop(rid, None)
            for res in [res for res, h in self._held.items() if h["run"] == rid]:
                self._held.pop(res, None)

    def _public(self, rid: str, r: dict) -> dict:
        return {"id": rid, "owner": r["owner"], "resources": list(r["resources"]),
                "expires_in": max(0.0, round(r["expires"] - self._now(), 1)), "runs": sorted(r["runs"]),
                "ended": self._over(r)}

    # --- runs ---
    def acquire(self, resources, run_id: str, who: str = "", reservation: str = "",
                owner: str = "") -> tuple[bool, dict]:
        """All-or-nothing. Returns (True, {}) on success, else (False, {resource: holder}). With
        `reservation`, the resources it holds count as free for this run, which must be `owner`'s;
        a reservation that is unknown, over or someone else's raises ReservationError."""
        resources = set(resources)
        with self._lock:
            self._expire()
            if reservation:
                r = self._resv.get(reservation)
                if r is None or self._over(r):
                    raise ReservationError(f"reservation {reservation} not found or expired")
                if r["owner"] != owner:
                    raise ReservationError(f"reservation {reservation} is not yours (it is {r['owner']}'s)")
            mine = {run_id, reservation} - {""}
            conflict = {r: dict(self._held[r]) for r in resources
                        if r in self._held and self._held[r]["run"] not in mine}
            if conflict:
                return False, conflict
            now = self._now()
            for r in resources:
                if r not in self._held:              # what the reservation holds stays the reservation's
                    self._held[r] = {"run": run_id, "who": who, "since": now}
            if reservation:
                self._resv[reservation]["runs"].add(run_id)
            return True, {}

    def release(self, run_id: str) -> None:
        with self._lock:
            for r in [r for r, h in self._held.items() if h["run"] == run_id]:
                self._held.pop(r, None)
            for resv in self._resv.values():
                resv["runs"].discard(run_id)
            self._expire()

    # --- reservations ---
    def reserve(self, resources, owner: str, ttl: float) -> tuple[bool, object]:
        """Hold `resources` for `owner` for `ttl` seconds, all or nothing. Returns (True, id), else
        (False, {resource: holder})."""
        resources = set(resources)
        with self._lock:
            self._expire()
            conflict = {r: dict(self._held[r]) for r in resources if r in self._held}
            if conflict:
                return False, conflict
            rid = "rsv-" + secrets.token_hex(6)
            now = self._now()
            who = f"reservation {rid} of {owner}"
            self._resv[rid] = {"owner": owner, "who": who, "resources": sorted(resources), "since": now,
                               "expires": now + ttl, "runs": set(), "ended": False}
            for r in resources:
                self._held[r] = {"run": rid, "who": who, "since": now}
            return True, rid

    def reservation(self, rid: str) -> dict | None:
        """A reservation that is still in force (None once it is over)."""
        with self._lock:
            self._expire()
            r = self._resv.get(rid)
            return self._public(rid, r) if r is not None and not self._over(r) else None

    def unreserve(self, rid: str) -> bool:
        """End a reservation: no new run may use it, and its resources go once no run under it is left."""
        with self._lock:
            r = self._resv.get(rid)
            if r is None or self._over(r):
                return False
            r["ended"] = True
            self._expire()
            return True

    def reservations(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [self._public(rid, r) for rid, r in sorted(self._resv.items())]

    # --- views and the emergency escape ---
    def force_release(self, resource: str) -> bool:
        with self._lock:
            h = self._held.pop(resource, None)
            rs = self._resv.get(h["run"]) if h is not None else None
            if rs is not None:
                rs["resources"] = [x for x in rs["resources"] if x != resource]
                if not rs["resources"]:
                    self._resv.pop(h["run"], None)
            return h is not None

    def held(self) -> list[dict]:
        with self._lock:
            self._expire()
            now = self._now()
            out = []
            for r, h in sorted(self._held.items()):
                item = {"resource": r, "run": h["run"], "who": h["who"], "held_for": round(now - h["since"], 1)}
                resv = self._resv.get(h["run"])
                if resv is not None:
                    item.update(reservation=h["run"], owner=resv["owner"],
                                expires_in=max(0.0, round(resv["expires"] - now, 1)))
                out.append(item)
            return out
