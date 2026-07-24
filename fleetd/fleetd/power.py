"""Fleet power control: wake nodes (Wake-on-LAN) / shut them down, and stream each
node's progress toward *serving* (or *offline*) as it happens.

This is the daemon-side twin of `tools/fleetpower.py` — same wake/poweroff primitives and
the same node file (`$DNC_NODES` / `~/dnc/fleet-nodes.yaml`), but instead of fire-and-forget
it runs a per-node poll loop and emits real-time events (phase + elapsed + ETA) that fleetd
relays over SSE to the Pi `/fleet-power` command.

Inventory note: power uses the fleet-nodes.yaml (name -> ip/mac/tier/never_sleep, plus
top-level `broadcast`/`ssh_user`), NOT fleetd's Host db. That file is the source of truth for
the MACs and the WoL broadcast, which the Host inventory doesn't carry. Keeping one file
avoids two drifting copies of "which nodes exist."

The pure helpers (select/normalise/phase/eta/event-shaping) are import-and-test friendly;
the network probes are small and injectable so the poll loop can be driven with fakes.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import yaml

# ETA budgets (seconds): how long a transition *typically* takes, so we can show a
# countdown. Measured on the BC-250 S3 fleet: WoL+POST+boot ~25s, boot->ssh ~10s,
# ssh->model-loaded+serving ~40s => ~75-90s end to end. OFF is a clean poweroff.
BUDGET_ON_S = 90
BUDGET_OFF_S = 45
POLL_S = 3
SSH_PORT = 22
DEFAULT_HEALTH_PORT = 8080  # llama-server; per-node override via `port:` in the node file
HEALTH_PATH = "/health"

CONFIG_CANDIDATES = [
    os.environ.get("DNC_NODES", ""),
    os.path.expanduser("~/dnc/fleet-nodes.yaml"),
    "fleet-nodes.yaml",
]

TERMINAL_ON = {"serving", "timeout"}
TERMINAL_OFF = {"offline", "timeout"}


# --- config + selection (pure) -----------------------------------------------------
def load_nodes(path: str | None = None) -> dict:
    for p in ([path] if path else CONFIG_CANDIDATES):
        if p and os.path.exists(p):
            with open(p) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["_path"] = p
            return cfg
    tried = ", ".join(c for c in CONFIG_CANDIDATES if c)
    raise FileNotFoundError(f"no node file found (tried: {tried}) — set $DNC_NODES")


def norm_name(tok: str) -> str:
    """'1' -> 'bc25001'; 'bc25005' -> 'bc25005'."""
    tok = tok.strip()
    return f"bc250{int(tok):02d}" if tok.isdigit() else tok


def norm_ip(tok: str) -> str:
    """'.106' or '106' -> '192.168.1.106'; a full dotted quad passes through."""
    tok = tok.strip()
    return tok if tok.count(".") == 3 else "192.168.1." + tok.lstrip(".")


def _split(v: str | None) -> list[str]:
    return [x for x in (v or "").replace(" ", "").split(",") if x]


def select_targets(
    cfg: dict,
    *,
    tier: str | None = None,
    nodes: str | None = None,
    ips: str | None = None,
    all_: bool = False,
) -> list[tuple[str, dict]]:
    """Union of the selectors, resolved against the node file. Deterministic order."""
    inv: dict = cfg.get("nodes", {})
    chosen: dict[str, dict] = {}
    if all_:
        chosen.update(inv)
    for t in _split(tier):
        chosen.update({n: d for n, d in inv.items() if str(d.get("tier")) == t})
    for tok in _split(nodes):
        name = norm_name(tok)
        if name in inv:
            chosen[name] = inv[name]
    if ips:
        want = {norm_ip(x) for x in _split(ips)}
        chosen.update({n: d for n, d in inv.items() if d.get("ip") in want})
    return sorted(chosen.items(), key=lambda kv: kv[0])


def partition_never_sleep(
    targets: list[tuple[str, dict]], *, force: bool
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """On OFF, hold back `never_sleep` nodes (e.g. the chassis fan controller) unless forced.
    Returns (to_act, skipped)."""
    if force:
        return targets, []
    act = [(n, d) for n, d in targets if not d.get("never_sleep")]
    skip = [(n, d) for n, d in targets if d.get("never_sleep")]
    return act, skip


# --- registry: register / de-register a node in the node file ----------------------
# The node file is the source of truth for /fleet-power (name -> ip/mac/tier + flags,
# plus top-level broadcast/ssh_user). These helpers edit it safely so nobody hand-edits
# YAML: validate the fields, refuse silent duplicates, and do a LINE-level upsert (the
# file's one-line-per-node flow style) so comments and layout survive. Every write is
# validated by re-parsing and applied atomically (temp + os.replace).
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TIERS_VALID = {"s0", "s1", "s2", "s3"}
DEFAULT_NODE_FILE = os.path.expanduser("~/dnc/fleet-nodes.yaml")


def resolve_config_path(create: bool = False) -> str:
    """First existing node file; with create=True, fall back to the default path (which
    register_node will create) instead of raising."""
    for p in CONFIG_CANDIDATES:
        if p and os.path.exists(p):
            return p
    if create:
        return os.environ.get("DNC_NODES") or DEFAULT_NODE_FILE
    tried = ", ".join(c for c in CONFIG_CANDIDATES if c)
    raise FileNotFoundError(f"no node file found (tried: {tried}) — set $DNC_NODES")


def normalize_mac(mac: str) -> str:
    hexmac = mac.replace(":", "").replace("-", "").strip().lower()
    if len(hexmac) != 12 or any(c not in "0123456789abcdef" for c in hexmac):
        raise ValueError(f"bad MAC: {mac!r} (want 6 hex octets)")
    return ":".join(hexmac[i:i + 2] for i in range(0, 12, 2))


def validate_ip(ip: str) -> str:
    ip = ip.strip()
    octs = ip.split(".")
    if len(octs) != 4 or any(not o.isdigit() or not 0 <= int(o) <= 255 for o in octs):
        raise ValueError(f"bad IP: {ip!r}")
    return ip


def _node_entry(ip: str, mac: str, tier: str, never_sleep: bool, port: int | None) -> dict:
    e: dict = {"ip": ip, "mac": mac, "tier": tier}
    if port:
        e["port"] = int(port)
    if never_sleep:
        e["never_sleep"] = True
    return e


def render_node_line(name: str, e: dict) -> str:
    """One-line flow entry matching the node file's convention."""
    parts = [f"ip: {e['ip']}", f'mac: "{e["mac"]}"', f"tier: {e['tier']}"]
    if e.get("port"):
        parts.append(f"port: {e['port']}")
    if e.get("never_sleep"):
        parts.append("never_sleep: true")
    return f"  {name}: {{ {', '.join(parts)} }}"


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _upsert_line(text: str, name: str, line: str) -> str:
    """Replace the existing `<name>:` line under `nodes:`, or insert it after the last
    node line. Creates a `nodes:` block if the file has none."""
    lines = text.splitlines()
    hdr = next((i for i, l in enumerate(lines) if re.match(r"^nodes\s*:\s*$", l)), -1)
    if hdr == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["nodes:", line]
        return "\n".join(lines) + "\n"
    pat = re.compile(rf"^\s+{re.escape(name)}\s*:")
    insert_at, i = hdr + 1, hdr + 1
    while i < len(lines):
        l = lines[i]
        if l.strip() == "" or l.lstrip().startswith("#") or l[:1] in (" ", "\t"):
            if pat.match(l):
                lines[i] = line
                return "\n".join(lines) + "\n"
            insert_at = i + 1
            i += 1
        else:
            break  # dedented line = end of the nodes block
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n"


def register_node(
    path: str, name: str, ip: str, mac: str, tier: str,
    *, never_sleep: bool = False, port: int | None = None, overwrite: bool = False,
) -> dict:
    """Validate + add (or replace) a node in the node file. Raises ValueError on bad
    input or on an existing name without overwrite."""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"bad node name: {name!r} (letters/digits/-/_ only)")
    if tier not in TIERS_VALID:
        raise ValueError(f"bad tier: {tier!r} (want one of {sorted(TIERS_VALID)})")
    ip, mac = validate_ip(ip), normalize_mac(mac)

    text = ""
    if os.path.exists(path):
        with open(path) as f:
            text = f.read()
        existing = (yaml.safe_load(text) or {}).get("nodes") or {}
        if name in existing and not overwrite:
            raise ValueError(f"{name} already registered (ip={existing[name].get('ip')}); pass overwrite to replace")
    else:
        text = ("# fleetpower node inventory (local, gitignored). Set broadcast/ssh_user "
                "for OFF/WoL.\n# broadcast: 192.168.1.255\n# ssh_user: youruser\nnodes:\n")

    entry = _node_entry(ip, mac, tier, never_sleep, port)
    new_text = _upsert_line(text, name, render_node_line(name, entry))
    if name not in ((yaml.safe_load(new_text) or {}).get("nodes") or {}):
        raise ValueError("internal error: node file edit did not register the node")
    _atomic_write(path, new_text)
    return {"name": name, **entry}


def deregister_node(path: str, name: str) -> dict:
    """Remove a node's line from the node file. Raises ValueError if it isn't registered."""
    with open(path) as f:
        text = f.read()
    existing = (yaml.safe_load(text) or {}).get("nodes") or {}
    if name not in existing:
        raise ValueError(f"{name} is not registered")
    removed = existing[name]
    pat = re.compile(rf"^\s+{re.escape(name)}\s*:")
    new_text = "\n".join(l for l in text.splitlines() if not pat.match(l)) + "\n"
    if name in ((yaml.safe_load(new_text) or {}).get("nodes") or {}):
        raise ValueError("internal error: node file edit did not de-register the node")
    _atomic_write(path, new_text)
    return {"name": name, **removed}


# --- state machine (pure) ----------------------------------------------------------
def phase_on(ssh_up: bool, health_code: int | None) -> str:
    """Classify an ON-bound node from two probes. `health_code` is None if the serving
    port didn't answer. Order matters: serving wins, then booted-but-not-ready, then boot."""
    if health_code == 200:
        return "serving"
    if ssh_up:
        return "loading"  # kernel + sshd up; model still loading (port not 200 yet)
    return "booting"      # WoL sent; host not yet reachable


def phase_off(ssh_up: bool, health_code: int | None) -> str:
    if not ssh_up and health_code is None:
        return "offline"
    return "stopping"


def eta_s(phase: str, elapsed: float, budget: float) -> int:
    """Seconds left, clamped >= 0. Terminal phases report 0."""
    if phase in TERMINAL_ON or phase in TERMINAL_OFF:
        return 0
    return max(0, round(budget - elapsed))


def node_event(name: str, d: dict, phase: str, elapsed: float, budget: float, detail: str = "") -> dict:
    return {
        "type": "node",
        "name": name,
        "ip": d.get("ip"),
        "tier": d.get("tier"),
        "phase": phase,
        "elapsed_s": round(elapsed, 1),
        "eta_s": eta_s(phase, elapsed, budget),
        "detail": detail,
    }


def summarize(states: dict[str, str], state: str, elapsed: float) -> dict:
    """Rollup across all tracked nodes for a heartbeat/final `summary` event."""
    done_key = "serving" if state == "on" else "offline"
    done = sum(1 for p in states.values() if p == done_key)
    timed_out = sum(1 for p in states.values() if p == "timeout")
    total = len(states)
    return {
        "type": "summary",
        "state": state,
        "total": total,
        "done": done,
        "timeout": timed_out,
        "pending": total - done - timed_out,
        "elapsed_s": round(elapsed, 1),
    }


# --- network primitives (small, injectable) ----------------------------------------
def wake(mac: str, broadcast: str) -> None:
    """Broadcast a Wake-on-LAN magic packet (ports 9 and 7)."""
    hexmac = mac.replace(":", "").replace("-", "").strip()
    if len(hexmac) != 12:
        raise ValueError(f"bad MAC: {mac!r}")
    pkt = b"\xff" * 6 + bytes.fromhex(hexmac) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for port in (9, 7):
            s.sendto(pkt, (broadcast, port))
    finally:
        s.close()


async def power_off_ssh(ip: str, user: str) -> tuple[bool, str]:
    """Graceful `sudo -n systemctl poweroff` over SSH. A clean poweroff usually drops the
    channel, so a closed connection counts as success."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
        f"{user}@{ip}", "sudo -n systemctl poweroff",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    text = (err or b"").decode() + (out or b"").decode()
    ok = proc.returncode == 0 or "closed" in text.lower()
    last = text.strip().splitlines()[-1] if text.strip() else ""
    return ok, last


async def probe_tcp(ip: str, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connect to ip:port succeeds within `timeout` (used for sshd = booted)."""
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def probe_health(ip: str, port: int, timeout: float = 3.0) -> int | None:
    """HTTP status of the serving health endpoint, or None if it didn't answer."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"http://{ip}:{port}{HEALTH_PATH}")
            return r.status_code
    except Exception:  # noqa: BLE001 — any connect/read failure = not answering yet
        return None


# --- the per-node poll loop (drives the state machine to a terminal phase) ----------
async def track_node(
    name: str,
    d: dict,
    state: str,
    *,
    budget_s: float,
    poll_s: float = POLL_S,
    emit: Callable[[dict], Any],
    now: Callable[[], float] = time.monotonic,
    ssh_probe: Callable[..., Any] = probe_tcp,
    health_probe: Callable[..., Any] = probe_health,
) -> None:
    """Poll one node until it reaches a terminal phase or the budget elapses, calling
    `emit(event)` on every phase change (plus a first and final event). Probes are
    injectable so this loop is unit-testable without a real network."""
    start = now()
    health_port = int(d.get("port", DEFAULT_HEALTH_PORT))
    terminal = TERMINAL_ON if state == "on" else TERMINAL_OFF
    classify = phase_on if state == "on" else phase_off
    last_phase: str | None = None

    # Seed with the intent so the UI shows the node immediately.
    emit(node_event(name, d, "waking" if state == "on" else "stopping", 0.0, budget_s))

    while True:
        elapsed = now() - start
        ssh_up = await ssh_probe(d["ip"], SSH_PORT)
        code = await health_probe(d["ip"], health_port) if ssh_up else None
        phase = classify(ssh_up, code)
        if elapsed >= budget_s and phase not in terminal:
            emit(node_event(name, d, "timeout", elapsed, budget_s,
                            detail=f"still {phase} after {budget_s:.0f}s"))
            return
        if phase != last_phase:
            emit(node_event(name, d, phase, elapsed, budget_s))
            last_phase = phase
        if phase in terminal:
            return
        await asyncio.sleep(poll_s)


async def watch(
    targets: list[tuple[str, dict]],
    state: str,
    cfg: dict,
    *,
    force: bool = False,
    budget_s: float | None = None,
    poll_s: float = POLL_S,
) -> AsyncIterator[dict]:
    """Fire the power action, then stream node/summary events until all nodes settle.

    Yields dicts: an initial `plan`, per-node `node` events on every phase change,
    periodic `summary` heartbeats, and a final `done`. Consumers (fleetd's SSE route)
    just json-encode each dict.
    """
    budget = budget_s if budget_s is not None else (BUDGET_ON_S if state == "on" else BUDGET_OFF_S)
    broadcast = cfg.get("broadcast", "255.255.255.255")
    user = cfg.get("ssh_user") or os.environ.get("USER", "root")

    act, skipped = partition_never_sleep(targets, force=force) if state == "off" else (targets, [])

    yield {
        "type": "plan",
        "state": state,
        "budget_s": budget,
        "nodes": [{"name": n, "ip": d.get("ip"), "tier": d.get("tier")} for n, d in act],
        "skipped": [{"name": n, "ip": d.get("ip"), "reason": "never_sleep"} for n, d in skipped],
        "config": cfg.get("_path"),
    }
    if not act:
        yield {"type": "done", "state": state, "total": 0, "done": 0, "timeout": 0}
        return

    # Fire the action. WoL is instant; poweroff we kick off concurrently.
    if state == "on":
        for n, d in act:
            try:
                wake(d["mac"], broadcast)
            except Exception as exc:  # noqa: BLE001
                yield {"type": "node", "name": n, "ip": d.get("ip"), "tier": d.get("tier"),
                       "phase": "error", "detail": f"WoL failed: {exc}", "elapsed_s": 0, "eta_s": 0}
    else:
        await asyncio.gather(*(power_off_ssh(d["ip"], user) for _, d in act), return_exceptions=True)

    start = time.monotonic()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    states: dict[str, str] = {n: ("waking" if state == "on" else "stopping") for n, _ in act}

    async def run_one(n: str, d: dict) -> None:
        await track_node(n, d, state, budget_s=budget, poll_s=poll_s, emit=queue.put_nowait)

    tasks = [asyncio.create_task(run_one(n, d)) for n, d in act]

    async def heartbeat() -> None:
        # Periodic summary so the UI can show elapsed/ETA even between phase changes.
        while True:
            await asyncio.sleep(poll_s)
            queue.put_nowait(summarize(states, state, time.monotonic() - start))

    hb = asyncio.create_task(heartbeat())
    all_done = asyncio.create_task(asyncio.gather(*tasks))

    async def sentinel() -> None:
        await all_done
        queue.put_nowait(None)

    sent = asyncio.create_task(sentinel())
    try:
        while (item := await queue.get()) is not None:
            if item.get("type") == "node":
                states[item["name"]] = item["phase"]
            yield item
    finally:
        hb.cancel()
        for t in (*tasks, sent):
            t.cancel()

    yield {**summarize(states, state, time.monotonic() - start), "type": "done"}
