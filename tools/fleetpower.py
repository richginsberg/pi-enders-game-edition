#!/usr/bin/env python3
"""Power fleet nodes ON (Wake-on-LAN) or OFF (graceful ssh poweroff), by tier / name / IP.

Turning off a whole tier by hand doesn't scale past a few nodes; this drives the fleet
from one command using an alias file (name -> ip/mac/tier).

  fleetpower.py --tier s3 --state on          # wake every S3 node
  fleetpower.py --tier s3 --state off         # shut down every S3 node (asks to confirm)
  fleetpower.py --nodes 1,2,3 --state on      # bc25001, bc25002, bc25003
  fleetpower.py --nodes bc25005 --state off
  fleetpower.py --ips .106,.123 --state off   # .106 == 192.168.1.106
  fleetpower.py --tier s3 --state off --dry-run
  fleetpower.py --all --state on
  fleetpower.py --sync                        # pull node list from fleetd, overwrite local file
  fleetpower.py --sync --fleetd http://cp:7431

Selectors combine (union). Config file, first found: $DNC_NODES,
~/dnc/fleet-nodes.yaml, ./fleet-nodes.yaml — override with --config.

ON  = broadcast a WoL magic packet (NIC must be WoL-armed; see tools/wol.py / provision).
OFF = ssh <ssh_user>@<ip> 'sudo -n systemctl poweroff' (needs passwordless sudo).
Nodes marked `never_sleep: true` (e.g. the chassis fan controller) are skipped on OFF
unless you pass --force.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
except ImportError:
    sys.exit("fleetpower needs PyYAML:  pip install pyyaml  (or: uv pip install pyyaml)")

CONFIG_CANDIDATES = [
    os.environ.get("DNC_NODES", ""),
    os.path.expanduser("~/dnc/fleet-nodes.yaml"),
    "fleet-nodes.yaml",
]


def load_config(path: str | None) -> dict:
    for p in ([path] if path else CONFIG_CANDIDATES):
        if p and os.path.exists(p):
            with open(p) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["_path"] = p
            return cfg
    sys.exit(f"no node file found (tried: {', '.join(c for c in CONFIG_CANDIDATES if c)}) — pass --config")


def norm_name(tok: str) -> str:
    """'1' -> 'bc25001'; 'bc25005' -> 'bc25005'."""
    tok = tok.strip()
    return f"bc250{int(tok):02d}" if tok.isdigit() else tok


def norm_ip(tok: str) -> str:
    """'.106' or '106' -> '192.168.1.106'; full IP passes through."""
    tok = tok.strip()
    if tok.count(".") == 3:
        return tok
    return "192.168.1." + tok.lstrip(".")


def select(cfg: dict, args) -> list[tuple[str, dict]]:
    nodes: dict = cfg.get("nodes", {})
    chosen: dict[str, dict] = {}
    if args.all:
        chosen.update(nodes)
    for t in _split(args.tier):
        chosen.update({n: d for n, d in nodes.items() if str(d.get("tier")) == t})
    for tok in _split(args.nodes):
        name = norm_name(tok)
        if name in nodes:
            chosen[name] = nodes[name]
        else:
            print(f"  ! unknown node: {tok} ({name})", file=sys.stderr)
    if args.ips:
        want = {norm_ip(t) for t in _split(args.ips)}
        chosen.update({n: d for n, d in nodes.items() if d.get("ip") in want})
    return sorted(chosen.items())


def _split(v: str | None) -> list[str]:
    return [x for x in (v or "").replace(" ", "").split(",") if x]


# --- --sync: pull the canonical inventory from fleetd and overwrite the local file ---
# fleetd (on the control-plane box) is the source of truth; this file is a standalone
# fallback. --sync fetches fleetd's /nodes and rewrites the local yaml so the two agree.
def _fleetd_base(args) -> str:
    return args.fleetd or os.environ.get("DNC_FLEETD_URL") or "http://localhost:7431"


def _pull_nodes(base: str) -> list[dict]:
    with urllib.request.urlopen(base.rstrip("/") + "/nodes", timeout=10) as r:
        data = json.load(r)
    if not isinstance(data, list):
        raise ValueError("unexpected /nodes response (not a list)")
    return data


def _render_node_line(name: str, d: dict) -> str:
    parts = [f"ip: {d['ip']}", f'mac: "{d.get("mac", "")}"', f"tier: {d.get('tier')}"]
    if d.get("chassis"):
        parts.append(f"chassis: {d['chassis']}")
    if d.get("port"):
        parts.append(f"port: {d['port']}")
    if d.get("never_sleep"):
        parts.append("never_sleep: true")
    return f"  {name}: {{ {', '.join(parts)} }}"


def _resolve_write_path(args) -> str:
    if args.config:
        return os.path.expanduser(args.config)
    for p in CONFIG_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return os.path.expanduser("~/dnc/fleet-nodes.yaml")


def sync_from_fleetd(args) -> int:
    base = _fleetd_base(args)
    path = _resolve_write_path(args)
    existing: dict = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = yaml.safe_load(f) or {}
    try:
        nodes = _pull_nodes(base)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[fleetpower] sync failed from {base}/nodes: {type(e).__name__}: {e}")

    # Preserve the local top-level WoL/ssh settings (fleetd's /nodes only returns nodes).
    out = [f"# fleet node inventory — synced from {base}/nodes (local edits overwritten)"]
    if existing.get("broadcast"):
        out.append(f"broadcast: {existing['broadcast']}")
    if existing.get("ssh_user"):
        out.append(f"ssh_user: {existing['ssh_user']}")
    out.append("nodes:")
    out += [_render_node_line(n["name"], n) for n in sorted(nodes, key=lambda x: x["name"])]
    text = "\n".join(out) + "\n"

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    print(f"[fleetpower] synced {len(nodes)} node(s) from {base} -> {path}")
    if not (existing.get("broadcast") and existing.get("ssh_user")):
        print("[fleetpower] note: set broadcast/ssh_user in that file for WoL/OFF to work")
    return 0


# --- WoL (inlined so this file is standalone; mirrors tools/wol.py) ---
def wake(mac: str, broadcast: str) -> None:
    hexmac = mac.replace(":", "").replace("-", "").strip()
    if len(hexmac) != 12:
        raise ValueError(f"bad MAC: {mac!r}")
    pkt = b"\xff" * 6 + bytes.fromhex(hexmac) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (9, 7):
        s.sendto(pkt, (broadcast, port))
    s.close()


def power_off(ip: str, user: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", f"{user}@{ip}",
         "sudo -n systemctl poweroff"],
        capture_output=True, text=True, timeout=30,
    )
    # A clean poweroff often drops the ssh channel -> non-zero / "closed by remote".
    ok = r.returncode == 0 or "closed" in (r.stderr + r.stdout).lower()
    return ok, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="fleetpower", description="Power fleet nodes on/off by tier/name/ip.")
    ap.add_argument("--state", choices=["on", "off"], help="power state (required unless --sync)")
    ap.add_argument("--sync", action="store_true",
                    help="pull node inventory from fleetd /nodes and OVERWRITE the local file, then exit")
    ap.add_argument("--sync-litellm", action="store_true",
                    help="tell fleetd to regenerate LiteLLM S3 entries from the registry + restart the gateway")
    ap.add_argument("--fleetd", help="fleetd base URL for --sync/--sync-litellm (default $DNC_FLEETD_URL or http://localhost:7431)")
    ap.add_argument("--tier", help="comma tiers, e.g. s3 or s1,s3")
    ap.add_argument("--nodes", help="comma names/numbers, e.g. 1,2,bc25005")
    ap.add_argument("--ips", help="comma IPs, e.g. .106,.123")
    ap.add_argument("--all", action="store_true", help="every node in the file")
    ap.add_argument("--config", help="node yaml (default: $DNC_NODES / ~/dnc/fleet-nodes.yaml)")
    ap.add_argument("--broadcast", help="override WoL broadcast address")
    ap.add_argument("--force", action="store_true", help="also power off never_sleep nodes")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the OFF confirmation")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.sync:
        return sync_from_fleetd(args)
    if args.sync_litellm:
        base = _fleetd_base(args)
        try:
            req = urllib.request.Request(base.rstrip("/") + "/litellm/sync", data=b"", method="POST")
            r = json.load(urllib.request.urlopen(req, timeout=90))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"[fleetpower] litellm-sync failed via {base}: {type(e).__name__}: {e}")
        print(f"[fleetpower] litellm synced: {r['entries']} entries for {len(r['s3_nodes'])} node(s); "
              f"gateway restarted={r['restarted']}")
        return 0
    if not args.state:
        ap.error("--state is required (on/off) unless you pass --sync")
    if not (args.all or args.tier or args.nodes or args.ips):
        ap.error("pick nodes with --tier / --nodes / --ips / --all")

    cfg = load_config(args.config)
    broadcast = args.broadcast or cfg.get("broadcast", "255.255.255.255")
    user = cfg.get("ssh_user") or os.environ.get("USER", "root")
    targets = select(cfg, args)
    if not targets:
        sys.exit("no matching nodes")

    # Guard never_sleep on OFF.
    skipped = []
    if args.state == "off" and not args.force:
        keep = [(n, d) for n, d in targets if d.get("never_sleep")]
        skipped = keep
        targets = [(n, d) for n, d in targets if not d.get("never_sleep")]

    print(f"[fleetpower] {args.state.upper()} {len(targets)} node(s) via {cfg['_path']}"
          + (f"  (broadcast {broadcast})" if args.state == "on" else ""))
    for n, d in targets:
        print(f"    {n:9s} {d.get('ip'):15s} {d.get('mac','')}  tier={d.get('tier')}")
    for n, d in skipped:
        print(f"    {n:9s} SKIPPED (never_sleep; use --force)")
    if args.dry_run:
        print("[fleetpower] dry-run — nothing sent")
        return 0

    if args.state == "off" and not args.yes:
        if input(f"Power OFF {len(targets)} node(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    def act(item):
        n, d = item
        try:
            if args.state == "on":
                wake(d["mac"], broadcast)
                return n, True, "magic packet sent"
            ok, msg = power_off(d["ip"], user)
            return n, ok, msg or "poweroff issued"
        except Exception as e:  # noqa: BLE001
            return n, False, f"{type(e).__name__}: {e}"

    rc = 0
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        for n, ok, msg in ex.map(act, targets):
            print(f"    {'✓' if ok else '✗'} {n}: {msg}")
            rc = rc or (0 if ok else 1)
    if args.state == "on":
        print("[fleetpower] wake packets sent — nodes take ~30-90s to serve (WoL + boot + model load)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
