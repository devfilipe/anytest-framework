"""Per-resource run locks. A run (test plan or ad-hoc) locks the bench resources it touches —
the target boards + the agents their drivers/actions use. Another run needing ANY held resource is
rejected immediately. In-memory, thread-safe; forced-unlock is the emergency escape."""
from __future__ import annotations

import threading
import time


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
    def __init__(self):
        self._held: dict = {}          # resource -> {"run","who","since"}
        self._lock = threading.Lock()

    def acquire(self, resources, run_id: str, who: str = "") -> tuple[bool, dict]:
        """All-or-nothing. Returns (True, {}) on success, else (False, {resource: holder})."""
        resources = set(resources)
        with self._lock:
            conflict = {r: dict(self._held[r]) for r in resources
                        if r in self._held and self._held[r]["run"] != run_id}
            if conflict:
                return False, conflict
            now = time.time()
            for r in resources:
                self._held[r] = {"run": run_id, "who": who, "since": now}
            return True, {}

    def release(self, run_id: str) -> None:
        with self._lock:
            for r in [r for r, h in self._held.items() if h["run"] == run_id]:
                self._held.pop(r, None)

    def force_release(self, resource: str) -> bool:
        with self._lock:
            return self._held.pop(resource, None) is not None

    def held(self) -> list[dict]:
        with self._lock:
            return [{"resource": r, "run": h["run"], "who": h["who"],
                     "held_for": round(time.time() - h["since"], 1)}
                    for r, h in sorted(self._held.items())]
