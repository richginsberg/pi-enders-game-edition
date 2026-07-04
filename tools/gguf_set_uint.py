#!/usr/bin/env python3
"""Overwrite a single scalar uint metadata value in a GGUF, in place.

Same-width overwrite only (uint32<->uint32 / uint64<->uint64), so no tensor
offsets shift — safe. Built to fix converter/runtime mismatches like Qwen3.6's
NextN layer being counted in `*.block_count` but not exported as tensors
(loader then fails on `missing tensor 'blk.<N>.*'`).

Usage:
  # inspect: show the key's current value + type, change nothing
  python3 tools/gguf_set_uint.py model.gguf qwen35moe.block_count
  # set it (writes in place; make a backup first if you want one)
  python3 tools/gguf_set_uint.py model.gguf qwen35moe.block_count 40
"""
from __future__ import annotations

import struct
import sys

U8, I8, U16, I16, U32, I32, F32, BOOL, STR, ARR, U64, I64, F64 = range(13)
_FMT = {U8: "<B", I8: "<b", U16: "<H", I16: "<h", U32: "<I", I32: "<i",
        F32: "<f", BOOL: "<?", U64: "<Q", I64: "<q", F64: "<d"}
_SIZE = {**{t: struct.calcsize(f) for t, f in _FMT.items()}}
_UINT = {U8, U16, U32, U64}


def _read(f, n):
    b = f.read(n)
    if len(b) != n:
        raise EOFError("truncated GGUF")
    return b


def _scalar(f, t):
    return struct.unpack(_FMT[t], _read(f, _SIZE[t]))[0]


def _string(f):
    return _read(f, _scalar(f, U64)).decode("utf-8", "replace")


def _skip_value(f, t):
    """Advance past a value of type t (used for keys we don't care about)."""
    if t == STR:
        _string(f)
    elif t == ARR:
        et = _scalar(f, U32)
        n = _scalar(f, U64)
        for _ in range(n):
            _skip_value(f, et) if et in (STR, ARR) else _read(f, _SIZE[et])
    else:
        _read(f, _SIZE[t])


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    path, key = sys.argv[1], sys.argv[2]
    new = int(sys.argv[3]) if len(sys.argv) == 4 else None

    with open(path, "rb") as f:
        if _read(f, 4) != b"GGUF":
            sys.exit("not a GGUF file (bad magic)")
        _scalar(f, U32)                 # version
        _scalar(f, U64)                 # tensor count
        n_kv = _scalar(f, U64)
        target = None                   # (value_offset, value_type)
        for _ in range(n_kv):
            k = _string(f)
            t = _scalar(f, U32)
            if k == key:
                target = (f.tell(), t)
                cur = _scalar(f, t) if t not in (STR, ARR) else _string(f) if t == STR else None
                break
            _skip_value(f, t)

    if target is None:
        sys.exit(f"key not found: {key}")
    off, t = target
    print(f"{key}: type={t} ({_FMT.get(t, '?')})  current={cur}")
    if new is None:
        return
    if t not in _UINT:
        sys.exit(f"refusing to set: {key} is not a uint scalar (type {t})")
    with open(path, "r+b") as f:
        f.seek(off)
        f.write(struct.pack(_FMT[t], new))
    print(f"set {key} = {new}  (in-place, {_SIZE[t]} bytes at offset {off})")


if __name__ == "__main__":
    main()
