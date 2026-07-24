"""Chain the fleet node registry into LiteLLM's config.

The gateway routes tier:s3 / tier:auto to per-node deployments listed in litellm-config.yaml
(api_base http://<ip>:<port>/v1). That was a third hand-maintained copy of the node list —
worst of the three, since a rebuilt node's new IP silently 404s until someone edits it.

`rewrite_config` regenerates ONLY the S3 node entries, fenced between marker comments, so
the hand-managed tier:s0 / tier:s1 entries, the disabled-grok comment, and general_settings
are preserved verbatim. It derives the per-entry template (model / api_key / context) from
an existing node entry, so no secrets or model ids are hardcoded here. `sync` does the I/O:
back up, write atomically, and (optionally) restart the gateway so it reloads.

Scope: tier:s3 nodes (the BC-250 qwen3.6 fleet). Non-s3 tiers stay hand-managed for now.
"""

from __future__ import annotations

import os
import re
import subprocess

import yaml

MARK_START = "  # >>> dnc-managed s3 node entries (fleetd litellm-sync — do not edit by hand) >>>"
MARK_END = "  # <<< dnc-managed s3 node entries <<<"
DEFAULT_PORT = 8080
_FALLBACK_TEMPLATE = {
    "model": "openai/qwen3.6-35b-reap", "api_key": "sk-noauth",
    "max_in": 262144, "max_out": 8192,
}


def _is_entry_start(line: str) -> bool:
    return line.lstrip().startswith("- model_name:")


def _is_node_entry(block: list[str]) -> bool:
    """A block is a generated S3 NODE entry (vs a hand-managed s0/s1 entry) iff it's an
    s3 squad member pointed at an http://<ip> api_base."""
    joined = "\n".join(block)
    return "dnc_squad: s3" in joined and bool(re.search(r"api_base:\s*http://\d", joined))


def _extract_template(block: list[str]) -> dict:
    j = "\n".join(block)
    def grab(pat, cast, default):
        m = re.search(pat, j)
        return cast(m.group(1)) if m else default
    return {
        "model": grab(r"model:\s*([^\s,}]+)", str, _FALLBACK_TEMPLATE["model"]),
        "api_key": grab(r"api_key:\s*([^\s,}]+)", str, _FALLBACK_TEMPLATE["api_key"]),
        "max_in": grab(r"max_input_tokens:\s*(\d+)", int, _FALLBACK_TEMPLATE["max_in"]),
        "max_out": grab(r"max_output_tokens:\s*(\d+)", int, _FALLBACK_TEMPLATE["max_out"]),
    }


def _entry(model_name: str, t: dict, ip: str, port: int, node: str) -> list[str]:
    return [
        f"  - model_name: {model_name}",
        f"    litellm_params: {{ model: {t['model']}, api_base: http://{ip}:{port}/v1, api_key: {t['api_key']} }}",
        f"    model_info: {{ id: s3-{node}, dnc_squad: s3, max_input_tokens: {t['max_in']}, max_output_tokens: {t['max_out']} }}",
    ]


def _generated_block(nodes: dict, t: dict, port_default: int) -> list[str]:
    out = [MARK_START]
    for name, d in sorted(nodes.items()):
        ip, port = d["ip"], int(d.get("port", port_default))
        out += _entry("tier:s3", t, ip, port, name)
        out += _entry("tier:auto", t, ip, port, name)
    out.append(MARK_END)
    return out


def _split_blocks(body: list[str]) -> tuple[list[str], list[list[str]]]:
    """Split model_list body into (pre-entry lines, per-entry blocks). Trailing comments/
    blanks after an entry attach to that entry's block (so a comment before the next entry
    stays with the previous one — e.g. the grok note rides with the tier:s1 entry)."""
    pre: list[str] = []
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for l in body:
        if _is_entry_start(l):
            if cur is not None:
                blocks.append(cur)
            cur = [l]
        elif cur is None:
            pre.append(l)
        else:
            cur.append(l)
    if cur is not None:
        blocks.append(cur)
    return pre, blocks


def _strip_markers(body: list[str]) -> list[str]:
    out, skip = [], False
    for l in body:
        if l.strip() == MARK_START.strip():
            skip = True
            continue
        if l.strip() == MARK_END.strip():
            skip = False
            continue
        if not skip:
            out.append(l)
    return out


def rewrite_config(text: str, nodes: dict, *, port_default: int = DEFAULT_PORT, template: dict | None = None) -> str:
    """Return `text` with the S3 node entries replaced by ones generated from `nodes`
    (name -> {ip, port?}). Everything outside the managed block is preserved verbatim.
    Raises ValueError if the result doesn't parse or would drop hand-managed entries."""
    lines = text.split("\n")
    ml = next((i for i, l in enumerate(lines) if re.match(r"^model_list\s*:", l)), None)
    if ml is None:
        raise ValueError("no `model_list:` in config")

    k = ml + 1
    while k < len(lines) and (lines[k].strip() == "" or lines[k][:1] in (" ", "\t")):
        k += 1
    prelude, body, trailer = lines[: ml + 1], lines[ml + 1 : k], lines[k:]

    body = _strip_markers(body)
    pre, blocks = _split_blocks(body)

    kept: list[str] = list(pre)
    found_tpl: dict | None = None
    kept_non_node = 0
    for b in blocks:
        if any(_is_entry_start(x) for x in b) and _is_node_entry(b):
            if found_tpl is None:
                found_tpl = _extract_template(b)
        else:
            kept.extend(b)
            if any(_is_entry_start(x) for x in b):
                kept_non_node += 1

    t = template or found_tpl or dict(_FALLBACK_TEMPLATE)
    result = "\n".join(prelude + kept + _generated_block(nodes, t, port_default) + trailer)

    # Safety: must parse, keep every hand-managed entry, and hold exactly 2 entries/node.
    parsed = yaml.safe_load(result)
    ml_out = parsed.get("model_list") if isinstance(parsed, dict) else None
    if not isinstance(ml_out, list):
        raise ValueError("rewrite produced no model_list")
    node_entries = [m for m in ml_out if (m.get("model_info") or {}).get("dnc_squad") == "s3"
                    and str((m.get("litellm_params") or {}).get("api_base", "")).startswith("http://")]
    if len(node_entries) != 2 * len(nodes):
        raise ValueError(f"expected {2 * len(nodes)} node entries, produced {len(node_entries)}")
    non_node = [m for m in ml_out if m not in node_entries]
    if len(non_node) != kept_non_node:
        raise ValueError(f"hand-managed entry count changed ({kept_non_node} -> {len(non_node)})")
    return result


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def sync(
    config_path: str, nodes_cfg: dict, *, restart: bool = True,
    restart_cmd: list[str] | None = None, port_default: int = DEFAULT_PORT,
) -> dict:
    """Regenerate the S3 node entries in litellm-config.yaml from the registry, back up the
    old file, write atomically, and (optionally) restart the gateway so it reloads."""
    s3 = {n: d for n, d in (nodes_cfg.get("nodes") or {}).items() if str(d.get("tier")) == "s3"}
    with open(config_path) as f:
        text = f.read()
    new_text = rewrite_config(text, s3, port_default=port_default)

    backup = f"{config_path}.bak-litellmsync"
    with open(backup, "w") as f:
        f.write(text)
    _atomic_write(config_path, new_text)

    restarted = False
    if restart:
        cmd = restart_cmd or ["sudo", "systemctl", "restart", "dnc-litellm"]
        subprocess.run(cmd, check=True, timeout=60)
        restarted = True
    return {
        "s3_nodes": sorted(s3), "entries": 2 * len(s3),
        "config": config_path, "backup": backup, "restarted": restarted,
    }
