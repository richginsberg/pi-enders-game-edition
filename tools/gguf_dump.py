#!/usr/bin/env python3
"""Dump a GGUF file's metadata + tensor list without the `gguf` pip package.

Parses the GGUF binary header directly (stdlib only), so it works on any box
that has the .gguf, regardless of what's pip-installed. Built to debug the
"missing tensor 'blk.N.*'" class of arch/convert mismatches (e.g. Qwen3.6
hybrid Gated-DeltaNet layers whose per-layer norms differ from full-attn ones).

Usage:
  python3 tools/gguf_dump.py model.gguf              # metadata + per-block tensor summary
  python3 tools/gguf_dump.py model.gguf --tensors    # also list every tensor name
  python3 tools/gguf_dump.py model.gguf --grep norm  # only tensor names matching a substring
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from collections import defaultdict

# GGUF metadata value types
U8, I8, U16, I16, U32, I32, F32, BOOL, STR, ARR, U64, I64, F64 = range(13)
_FMT = {U8: "<B", I8: "<b", U16: "<H", I16: "<h", U32: "<I", I32: "<i",
        F32: "<f", BOOL: "<?", U64: "<Q", I64: "<q", F64: "<d"}
_SIZE = {U8: 1, I8: 1, U16: 2, I16: 2, U32: 4, I32: 4, F32: 4, BOOL: 1,
         U64: 8, I64: 8, F64: 8}


class R:
    def __init__(self, f):
        self.f = f

    def raw(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("truncated GGUF (file ends mid-header)")
        return b

    def scalar(self, t):
        return struct.unpack(_FMT[t], self.raw(_SIZE[t]))[0]

    def string(self):
        n = self.scalar(U64)
        return self.raw(n).decode("utf-8", "replace")

    def value(self, t):
        if t == STR:
            return self.string()
        if t == ARR:
            et = self.scalar(U32)
            n = self.scalar(U64)
            # summarize big arrays (vocab, merges) instead of materializing them
            if n > 16 or et == ARR:
                # still must consume the bytes to stay aligned
                for _ in range(n):
                    self.value(et) if et in (STR, ARR) else self.raw(_SIZE[et])
                return f"<array {n} x type{et}>"
            return [self.value(et) if et in (STR, ARR) else self.scalar(et) for _ in range(n)]
        return self.scalar(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tensors", action="store_true", help="list every tensor name")
    ap.add_argument("--grep", help="only tensor names containing this substring")
    a = ap.parse_args()

    with open(a.path, "rb") as f:
        r = R(f)
        if r.raw(4) != b"GGUF":
            sys.exit("not a GGUF file (bad magic)")
        ver = r.scalar(U32)
        n_tensors = r.scalar(U64)
        n_kv = r.scalar(U64)
        print(f"GGUF v{ver}  tensors={n_tensors}  metadata_kv={n_kv}\n== metadata ==")

        meta = {}
        for _ in range(n_kv):
            k = r.string()
            t = r.scalar(U32)
            meta[k] = r.value(t)
        for k in sorted(meta):
            v = meta[k]
            print(f"  {k} = {v}")

        # tensor infos
        names = []
        for _ in range(n_tensors):
            name = r.string()
            nd = r.scalar(U32)
            dims = [r.scalar(U64) for _ in range(nd)]
            r.scalar(U32)   # ggml type
            r.scalar(U64)   # offset
            names.append((name, dims))

    # per-block summary: which suffixes exist on each blk.N, and the block range
    blk = defaultdict(set)
    max_blk = -1
    non_blk = []
    for name, _dims in names:
        m = re.match(r"blk\.(\d+)\.(.+)", name)
        if m:
            i = int(m.group(1))
            blk[i].add(m.group(2))
            max_blk = max(max_blk, i)
        else:
            non_blk.append(name)

    print(f"\n== blocks ==  count={len(blk)}  max index={max_blk}")
    # show the suffix set for each block; collapse runs of identical sets
    prev = None
    for i in range(max_blk + 1):
        suffixes = tuple(sorted(blk.get(i, ())))
        tag = "MISSING (no tensors!)" if not suffixes else ""
        if suffixes != prev:
            print(f"  blk.{i}: {', '.join(suffixes) or '<none>'} {tag}")
            prev = suffixes
        # else: same layout as previous block, elide

    # attn_norm presence map — the tensor the loader complained about
    have = sorted(i for i in range(max_blk + 1) if "attn_norm.weight" in blk.get(i, ()))
    lack = sorted(i for i in range(max_blk + 1) if "attn_norm.weight" not in blk.get(i, ()))
    print(f"\n== attn_norm.weight ==\n  present on blocks: {have}\n  ABSENT on blocks:  {lack}")

    if a.tensors or a.grep:
        print("\n== tensors ==")
        for name, dims in names:
            if a.grep and a.grep not in name:
                continue
            print(f"  {name}  {dims}")


if __name__ == "__main__":
    main()
