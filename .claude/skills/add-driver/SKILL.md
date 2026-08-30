---
name: add-driver
description: Add a new access channel TYPE to the framework (the serial/ip pattern). Use when a check needs a way to reach a board that the built-in serial/ip channels don't cover.
---

# Add an access channel (driver type)

A **driver** is a comm channel a check reaches a board through. The bench wires it to a board under
an **alias** the check declares (`console`, `craft`, `mgmt`, …); the runner **skips** a check whose
alias the bench doesn't provide. Each driver has a **type** that selects the channel class:

- `serial` → `SerialChannel` — console over an agent (ssh+serial / ser2net)
- `ip` → `IpChannel` — network vantage; with an agent = the craft-style vantage, without an agent =
  host/container `nmap`. Exposes `.ip` (the target address).
- the implicit `host` (`HostProbe`) — always available, local vantage

Adding a new *driver instance* (a new alias like `oob`) is bench/inventory config — no code. Adding
a new **channel type** (this skill) is a code change.

## Where channels live

```
atf/access/channels/
  base.py       Channel base — `type` attr + open/close, send/expect/sh helpers
  console.py    SerialChannel  (type = "serial")
  ip.py         IpChannel      (type = "ip") — carries .ip; ping/tcp/scan/nse/sh
```

## Steps

1. **Create the channel** `atf/access/channels/<name>.py`, subclassing `Channel` (`base.py`). Set
   the class attr `type = "<name>"` and implement connect + the probe primitives your checks call.
   Keep it black-box (reach the board from outside; don't log in to read config as a shortcut).
2. **Dispatch by type** in `atf/core/runner.py` `_build_ctx`: it loops `board.drivers` and builds a
   channel per alias from `cfg["type"]`. Add a branch for your type (mirror how `serial` and `ip`
   are wired).
3. **ctx is alias-keyed** — there is no fixed attribute. `ctx.<alias>` resolves to the channel the
   bench wired at that alias (`Ctx.__getattr__` in `core/model.py`); a check reads `ctx.<alias>` and
   its methods (e.g. `ctx.mgmt.ip`, `ctx.console.expect(...)`).
4. **Availability gating.** `runner.available_drivers()` decides an alias is available from the bench
   wiring (agent presence + type). Fit your type into that logic so a check declaring the alias runs
   only where it's bound and is **skipped** elsewhere.
5. **Inventory driver type (optional).** To let users pick your type in the UI, seed it as an
   `inv_driver` with its prop schema — see the built-in `serial`/`ip` seeds in `store/db.py`.
6. **Container path (optional).** An `ip`-without-agent channel runs in the `atf-mgmt` container via
   `access/mgmt/{dispatch,worker}.py`. If your type also runs there, wire it into the worker.

## Verify

```bash
make check
make test
# a check declaring drivers=("<alias>",) should list + run on a bench that wires that alias to your
# type, and be SKIPPED on one that doesn't.
```

Do **not** make a missing driver a hard failure — unavailable ⇒ skipped is the contract.
