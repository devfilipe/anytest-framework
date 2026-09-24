"""Guided manual-check helper: print steps, capture the operator's observation.

The capture is pluggable via `set_prompter(fn)`: the CLI uses the default stdin flow; the
web dashboard installs a prompter that collects the answer through a form (no blocking
`input()`). `fn(instructions, default_severity, check_id) -> Result`."""
from __future__ import annotations

from atf.core.model import Result, Severity, Verdict

_PROMPTER = None


def set_prompter(fn) -> None:
    """Install (or clear, with None) the active prompter. Not thread-safe by design — one
    run at a time drives manual capture."""
    global _PROMPTER
    _PROMPTER = fn


def _verdict(ans: str) -> Verdict:
    return {"pass": Verdict.PASS, "gap": Verdict.GAP}.get(ans, Verdict.MANUAL)


def result_from(observation: str, verdict: Verdict, default_severity: Severity) -> Result:
    sev = default_severity if verdict == Verdict.GAP else Severity.INFO
    return Result(verdict, sev, title="manual observation", detail=observation,
                  evidence=observation)


def prompt(instructions: str, default_severity: Severity = Severity.MEDIUM,
           check_id: str = "") -> Result:
    if _PROMPTER is not None:
        return _PROMPTER(instructions, default_severity, check_id)
    import sys
    if not sys.stdin or not sys.stdin.isatty():
        # non-interactive context (agent worker, CI, redirected stdin): no operator to ask —
        # return a clean MANUAL verdict instead of crashing on input()/EOF
        return result_from("manual check — run it interactively to capture a verdict "
                           "(no prompter available in this run)", Verdict.MANUAL, default_severity)
    print("\n=== MANUAL CHECK ===")
    print(instructions)
    obs = input("Observation (what did you see?): ").strip()
    ans = input("Verdict [pass/gap/skip] (default skip=manual-pending): ").strip().lower()
    return result_from(obs, _verdict(ans), default_severity)
