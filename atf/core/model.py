"""Core data model: verdicts, severity, Result, CheckSpec, run Ctx."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycles
    from atf.core.inventory import Board
    from atf.access.channels.base import Channel
    from atf.access.host import HostProbe
    from atf.access.actions import Actions


class Verdict(str, Enum):
    PASS = "pass"        # verified compliant
    GAP = "gap"          # confirmed non-compliance / finding
    MANUAL = "manual"    # awaiting operator sign-off
    ERROR = "error"      # could not run/determine
    SKIPPED = "skipped"  # required vector unavailable
    NA = "na"            # requirement does not apply


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Result:
    verdict: Verdict
    severity: Optional[Severity] = None      # None => runner falls back to the check's default
    title: str = ""
    detail: str = ""
    evidence: str = ""                       # path relative to reports/, or inline note
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckSpec:
    """Metadata + callable for a single check (populated by @register). A test declares the
    framework capabilities it needs — `drivers` (comm channels) and `actions` (node actions);
    the bench provides them and the runner gates on availability."""
    id: str
    drivers: frozenset[str]                  # comm drivers needed (console/craft/mgmt); empty => host-only
    actions: frozenset[str] = frozenset()    # node actions needed (power-cycle, …); empty => none
    requirements: tuple[str, ...] = ()       # ADVISORY suggestion only — the Suite owns requirement↔test mapping
    mode: str = "auto"                       # "auto" | "manual"
    severity: Severity = Severity.MEDIUM     # default severity of a gap
    title: str = ""
    fn: Callable[["Ctx"], Result] = None
    disruptive: bool = False                 # reboots/mutates the board -> only run when named by --id
    model: str = ""                          # "" = common (any board); else a model slug (e.g. router_x_lite)
    path: str = ""                           # source file (set for Markdown manual tests; .py use fn.__module__)


@dataclass
class Ctx:
    """Per-(board) run context handed to a check. Comm drivers are alias-keyed: a check reaches
    one as ``ctx.<alias>`` (e.g. ``ctx.mgmt`` / ``ctx.console`` / ``ctx.craft`` / ``ctx.oob``),
    present if and only if the bench wired a driver with that alias. Each driver object carries its own props
    (e.g. ``ctx.mgmt.ip``) and probe methods. ``ctx.host`` is always present."""
    board: "Board"
    host: "HostProbe"
    out_root: Path
    drivers: dict[str, "Channel"] = field(default_factory=dict)   # alias -> Channel (wired drivers)
    actions: Optional["Actions"] = None       # node actions (power-cycle, …) configured on the bench
    check_id: str = ""                        # set by the runner per check; names evidence

    def __getattr__(self, name: str):
        # alias-keyed driver access; only reached when `name` isn't a real attribute
        drivers = self.__dict__.get("drivers")
        if drivers and name in drivers:
            return drivers[name]
        raise AttributeError(name)

    @property
    def available_drivers(self) -> set[str]:
        """Comm-driver aliases wired on this ctx (host is always implicitly available)."""
        return set(self.drivers)

    @property
    def available_actions(self) -> set[str]:
        """Node actions the bench configured for this board."""
        return set(self.actions.available()) if self.actions else set()

    def write_evidence(self, text: str, slug: Optional[str] = None) -> str:
        """Persist raw evidence; return path relative to out_root (for the record).
        The filename defaults to the running check id (`<check-id>-<board>.txt`), so
        evidence is named consistently without each check hand-crafting a slug."""
        slug = (slug or self.check_id or "evidence").replace(":", "-")
        rel = Path("evidence") / f"{slug}-{self.board.name}.txt"
        p = self.out_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(rel)
