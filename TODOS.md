# TODOs

Deferred features and larger pieces of work, kept out of the commit history until picked up.

## Agent-as-node — reach a node over the outbound agent hub (no inbound SSH)

**Motivation.** A node is reached today by **outbound SSH from the server** (`AgentConn` →
paramiko → `host:ssh_port`), so a node must be SSH-reachable *from the server*. A developer's or
tester's machine (the one accessing the web UI) usually is not — it sits behind NAT/firewall with
no reachable sshd. Yet that machine **already** holds an outbound long-poll session on the AgentHub
(`atf agent`, `/api/agents/*`). The idea: let a registered agent also serve as the **vantage** for
a node, dispatching driver ops over that existing channel instead of SSHing to it.

The configurable **SSH port** on nodes (already shipped) covers the reverse-SSH-tunnel workaround;
this feature covers the NAT case with no tunnel at all, and makes the tester's machine a first-class
bench node — probes then run *from where the tester physically is*.

### Design

- **Node ↔ agent binding.** Add a `transport` field to `inv_agent`: `ssh` (default, current) or
  `hub`. When `hub`, the node points at a registered agent (`agent_ref`) — no host/port/creds.
- **Reverse RPC over the long-poll.** Generalize the poll channel (which today delivers commands and
  receives tree/catalog/file results) into request/reply:
  - Server: `AgentHub.request(agent_id, {op, ...}, timeout)` enqueues and blocks on a Future keyed
    by `req_id`.
  - Agent: on receiving an `op` in its poll loop, executes it **locally** and
    `POST /api/agents/{aid}/reply {req_id, rc, out, err}`.
  - Server: resolves the Future → returns to the caller.
- **`AgentConn` becomes an interface with two implementations.** `SshAgentConn` (today's paramiko
  code, renamed) and `HubAgentConn(hub, agent_id)` with the same surface
  (`run`/`ping`/`tcp_scan`/`serial_stream`/`close`). A factory picks by `agent.transport`. Checks and
  channels are unchanged — they only ever talk to `AgentConn`.

### Increments

- **3a — one-shot ops over the hub** (`run`, `ping`, `tcp_scan`): makes the machine a usable node for
  **node actions (the Run button), reachability, and command-based checks**. `IpChannel` *with an
  agent* uses exactly `ping`/`tcp`/`scan`, so it works immediately. Effort: medium.
- **3b — serial console bridging over the hub** (bidirectional streaming `send`/`recv`): needs a
  streaming sub-protocol (chunked, or a dedicated WS/SSE). This is the hard, stateful part — defer
  until 3a proves out. Console stays SSH/ser2net until then.

### Security

The hub RPC executes arbitrary commands on the tester's machine — but that is **already** the trust
model of `atf agent` (it runs pushed check code). Still, make it explicit opt-in: an
`atf agent --allow-node` flag (a working-tree agent does not silently become a command executor),
gate by owner/admin, and log every op.

### UI

Node editor: a **Transport** select (SSH ↔ Hub agent, choosing among connected agents). The Test/Run
buttons already go through `AgentConn`, so they use the hub transparently.
