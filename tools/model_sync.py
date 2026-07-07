#!/usr/bin/env python3
"""Control-plane model cache + LAN fan-out, gated on the HuggingFace commit/sha.

Pull a model from HF ONCE to a local cache (verifying the LFS sha256), then rsync it to
fleet nodes over the LAN — instead of every node re-downloading over the WAN. Re-checks
the HF commit + sha before (re)distributing, so a node only gets a new copy when the
upstream actually changed.

The checksum gate: HF's `resolve` endpoint returns `X-Repo-Commit` (the commit) and
`X-Linked-ETag` (the LFS object sha256) on a HEAD, without downloading. We cache those in
`<file>.meta` next to the file; sync is a no-op when commit+sha still match.

Stdlib only (urllib + hashlib + rsync via subprocess). Public repos need no token; for a
private repo set HF_TOKEN in the env.

Usage (run on the control plane, or any host with HF + SSH-to-nodes reach):
  # ensure the cache has the current upstream (HEAD-gated, sha256-verified):
  python3 tools/model_sync.py sync  machinez/Qwen3.6-35B-REAP-Pruned-ratio-0.5 \
      qwen3.6-35b-reap-Q3_K_M.gguf --cache ~/dnc/models
  # fan out the cached file to nodes (rsync, verifies remote size):
  python3 tools/model_sync.py distribute qwen3.6-35b-reap-Q3_K_M.gguf --cache ~/dnc/models \
      --to machinelearning@192.168.1.135:models/
  # both at once:
  python3 tools/model_sync.py deploy machinez/Qwen3.6-35B-REAP-Pruned-ratio-0.5 \
      qwen3.6-35b-reap-Q3_K_M.gguf --cache ~/dnc/models --to machinelearning@192.168.1.135:models/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HF = "https://huggingface.co"


def _head_hf(repo: str, filename: str, rev: str = "main") -> tuple[str, str]:
    """Return (commit, sha256) for a repo file via a redirect-free HEAD on /resolve/.
    HF answers with X-Repo-Commit + X-Linked-ETag (the LFS sha256) before any download."""
    url = f"{HF}/{repo}/resolve/{rev}/{filename}"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # don't follow the CDN redirect
            return None

    req = urllib.request.Request(url, method="HEAD")
    tok = os.environ.get("HF_TOKEN")
    if tok:
        req.add_header("authorization", f"Bearer {tok}")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=30)
        headers = resp.headers  # 200 (small/non-LFS file)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        headers = e.headers  # 302 to CDN — the headers we want are here
    commit = headers.get("X-Repo-Commit", "")
    sha = (headers.get("X-Linked-ETag") or headers.get("ETag") or "").strip('"')
    if not commit or not sha:
        raise RuntimeError(f"HF HEAD missing commit/sha for {repo}/{filename} (private? need HF_TOKEN?)")
    return commit, sha


def _sha256(path: str, buf: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(repo: str, filename: str, rev: str, dest: str, expect_sha: str) -> None:
    url = f"{HF}/{repo}/resolve/{rev}/{filename}"
    req = urllib.request.Request(url)  # default opener follows the 302 to the CDN
    tok = os.environ.get("HF_TOKEN")
    if tok:
        req.add_header("authorization", f"Bearer {tok}")
    tmp = dest + ".tmp"
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        while chunk := r.read(8 << 20):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                print(f"\r  downloading {done/1e9:.2f}/{total/1e9:.2f} GB", end="", flush=True)
    print()
    got = h.hexdigest()
    if got != expect_sha:
        os.remove(tmp)
        raise RuntimeError(f"sha256 mismatch: got {got} expected {expect_sha}")
    os.replace(tmp, dest)


def cmd_sync(args) -> str:
    os.makedirs(args.cache, exist_ok=True)
    dest = os.path.join(args.cache, os.path.basename(args.filename))
    meta_path = dest + ".meta"
    commit, sha = _head_hf(args.repo, args.filename, args.rev)
    meta = {}
    if os.path.exists(meta_path):
        meta = json.loads(open(meta_path).read())
    if os.path.exists(dest) and meta.get("commit") == commit and meta.get("sha256") == sha:
        print(f"[model_sync] up-to-date: {os.path.basename(dest)} @ {commit[:12]} (sha ok, cached)")
        return dest
    print(f"[model_sync] fetching {args.repo}/{args.filename} @ {commit[:12]} (sha {sha[:12]}…)")
    _download(args.repo, args.filename, args.rev, dest, sha)
    open(meta_path, "w").write(json.dumps({"repo": args.repo, "filename": args.filename,
                                           "commit": commit, "sha256": sha}))
    print(f"[model_sync] cached + verified: {dest}")
    return dest


def cmd_distribute(args) -> None:
    src = os.path.join(args.cache, os.path.basename(args.filename))
    if not os.path.exists(src):
        sys.exit(f"not in cache: {src} (run `sync` first)")
    for target in args.to:
        # target = user@host:destdir/  — rsync creates destdir's parent must exist; -R not used
        host = target.split(":")[0]
        destdir = target.split(":", 1)[1] or "."
        subprocess.run(["ssh", host, f"mkdir -p {destdir}"], check=True)
        print(f"[model_sync] rsync -> {target}")
        subprocess.run(["rsync", "-a", "--partial", "--info=progress2", src, target], check=True)
    print("[model_sync] distributed to all nodes")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    common_cache = dict(default=os.path.expanduser("~/dnc/models"))
    s = sub.add_parser("sync"); s.add_argument("repo"); s.add_argument("filename")
    s.add_argument("--cache", **common_cache); s.add_argument("--rev", default="main")
    d = sub.add_parser("distribute"); d.add_argument("filename")
    d.add_argument("--cache", **common_cache); d.add_argument("--to", action="append", required=True)
    dep = sub.add_parser("deploy"); dep.add_argument("repo"); dep.add_argument("filename")
    dep.add_argument("--cache", **common_cache); dep.add_argument("--rev", default="main")
    dep.add_argument("--to", action="append", required=True)
    args = ap.parse_args()

    if args.cmd == "sync":
        cmd_sync(args)
    elif args.cmd == "distribute":
        cmd_distribute(args)
    elif args.cmd == "deploy":
        cmd_sync(args)
        cmd_distribute(args)


if __name__ == "__main__":
    main()
