# Converting Qwen3.6 (`qwen3_5_moe`, hybrid + MTP) to GGUF

Runbook for quantizing the Qwen3.6 family — e.g. **RangerX/Qwen3.6-35B-REAP-Pruned-ratio-0.5**
(the S3 shootout candidate, see [model-selection-2026.md](model-selection-2026.md)) — to GGUF for
llama.cpp. This arch (`qwen3_5_moe`, "Gated DeltaNet" hybrid attention + MoE + a NextN/MTP
speculative head) was still churning through llama.cpp/Transformers support in mid-2026, so a
naive `convert → quantize` hits three separate walls. All three, in order, plus fixes:

## Prereqs
- **Transformers must be new enough** for `qwen3_5_moe` **and** the `TokenizersBackend` tokenizer
  class, or `convert_hf_to_gguf.py` dies at the tokenizer step with
  `ValueError: Tokenizer class TokenizersBackend does not exist or is not currently imported`.
  `pip install -U transformers tokenizers` in the convert env.
- **Use ONE llama.cpp build for the whole pipeline** (convert + imatrix + quantize) and match it
  on the runtime/serving side. Mixing builds for this fast-moving arch produces missing-tensor
  errors (we saw a 0531 runtime reject a 0621-converted GGUF). Prefer a current master; pin the
  BC-250 container's `LLAMA_CPP_REF` to the same commit you validate against.

## Step 1 — convert HF → f16 GGUF
```bash
python3 llama.cpp/convert_hf_to_gguf.py <hf-model-dir> \
  --outfile ~/quantize/qwen3.6-35b-reap-f16.gguf --outtype f16
```
Confirm it ends with a success line (a run that aborts mid-way leaves a truncated GGUF).
`WARNING: Unknown RoPE type: default` is benign (mrope sections are still read).

## Step 2 — fix the NextN/MTP metadata mismatch (THE gotcha)
The converter counts the **NextN (multi-token-prediction) speculative layer** in
`*.block_count` but **does not export its tensors**. Metadata and payload disagree, so the loader
fails. Diagnose + patch with the two stdlib tools in [`tools/`](../tools):

```bash
# see the mismatch: block_count says 41 but tensors only go blk.0..blk.39
python3 tools/gguf_dump.py ~/quantize/qwen3.6-35b-reap-f16.gguf | sed -n '/metadata/,/blocks/p'
```
You'll see `qwen35moe.block_count = 41`, `qwen35moe.nextn_predict_layers = 1`, and
`== blocks == count=40 max index=39` (no `blk.40.*` tensors exist).

The runtime treats **the last `nextn_predict_layers` blocks as NextN** and demands `blk.<N>.nextn.*`
on them. Since our NextN layer was never exported, we drop it: set the real layer count **and**
zero the NextN count (same-width uint32 in-place edits — no tensor offsets move):
```bash
cp ...f16.gguf ...f16.bak.gguf          # cheap insurance
python3 tools/gguf_set_uint.py ...f16.gguf qwen35moe.block_count 40          # 41 -> 40 (real layers only)
python3 tools/gguf_set_uint.py ...f16.gguf qwen35moe.nextn_predict_layers 0  # 1  -> 0  (no NextN expected)
```
> Order matters conceptually: setting only `block_count=40` just makes the runtime think block 39
> (a real layer) is the NextN head → `missing tensor 'blk.39.nextn.eh_proj.weight'`. You must ALSO
> zero `nextn_predict_layers`. After both, metadata matches the payload: 40 normal blocks, no NextN.

Dropping the NextN head only forfeits MTP speculative decoding (which llama.cpp doesn't run for
standard decoding anyway) — normal generation is unaffected.

**Canonical alternative:** re-convert on a newer llama.cpp master that exports the NextN tensors (or
sets `block_count` correctly) — no hand-patching. Use this if the patched model's output looks off.

## Step 3 — imatrix (optional, improves aggressive quants)
```bash
llama-imatrix -m ~/quantize/qwen3.6-35b-reap-f16.gguf -f calibration.txt -o imatrix.dat --chunks 100
```
`calibration.txt` is any ~0.5–2 MB diverse UTF-8 corpus (code + prose); the community
`calibration_datav3.txt` / `groups_merged.txt` work well. imatrix loads the **f16** (~38 GB for the
19B-active REAP) — needs the RAM/VRAM to hold it, else compute the imatrix from a Q8_0 first, or skip.

## Step 4 — quantize
```bash
llama-quantize --imatrix imatrix.dat ~/quantize/qwen3.6-35b-reap-f16.gguf \
  ~/quantize/qwen3.6-35b-reap-Q3_K_M.gguf Q3_K_M
```
Q3_K_M ≈ 9.3 GB (fits BC-250 12 GB with long context); Q4_K_S ≈ 10.7 GB (tighter context). See the
VRAM math in [model-selection-2026.md](model-selection-2026.md).

## Step 5 — runtime sanity + benchmark
The GGUF carries `ssm_*` / `attn_gate` tensors on the linear layers (the Gated-DeltaNet hybrid), so
**the serving llama.cpp must be new enough to run the SSM/linear-attention path** — a Q3 that
converts fine on the dev box can still fail to load on the BC-250 if the container's llama.cpp lags.
Pin the container `LLAMA_CPP_REF` to the validated commit, then:
```bash
python3 tools/model_bench.py --base http://<node>:8080/v1 --model <alias> --tag qwen36-reap --run-code
python3 tools/model_bench.py --compare qwen3coder-current qwen36-reap
```

## Step 6 — serving budget on the BC-250: host RAM, not VRAM, is the ceiling
The VRAM math above (weights + KV fit 12 GB) is necessary but **not sufficient**. The BC-250's
16 GB is BIOS-partitioned into ~12 GB VRAM + **only 3.5 GB host RAM**, and llama.cpp's per-slot
KV/compute/GTT-spill buffers live in that **host** partition and scale with `--parallel × ctx`.

Observed 2026-07-04: RangerX Q3_K_M loaded fine, smoke-tested fine (~66 tok/s, coherent), then
**`Exited (137)` under concurrent long-context load** — `dmesg` showed `Out of memory: Killed
process (llama-server)`, `global_oom`. The GPU was healthy; the **3.5 GB host** ran out because
the server was running `n_slots = 4`, each slot holding 4–7k-token contexts. Node stayed pingable
/ SSH-able; only port 8080 died (`connection refused`).

Rules for serving any model on the BC-250:
- **Cap `--parallel 1`** (single slot) and keep **`-c` modest (≤16384)**. Four slots ≈ 4× the
  host-side buffers → OOM.
- **Do NOT use `--no-mmap`** — it mallocs the full weights (~9.3 GB) into host RAM → instant OOM.
  Keep mmap so the weights stay resident on the GPU side.
- **Watch `free -h` under load**; if `available` trends toward 0, back off ctx/parallel.
- **Agentic harnesses (Terminal-Bench, etc.) run `--n-concurrent 1`** against this node — long
  agentic contexts × concurrency is the OOM trigger for hybrid models.
- **The fix for hybrids on the BC-250: dynamic 512 MB VRAM BIOS split (confirmed 2026-07-04).** On
  the *default* 12 GB-VRAM / 3.5 GB-host split, the hybrid OOMs under concurrency — its
  `kv_unified='false'` per-slot KV + Gated-DeltaNet SSM recurrent state are host-allocated and scale
  with `slots × ctx` (A/B: plain `qwen3moe` Qwen3-Coder survives the identical config; the hybrid
  doesn't). **Repartition the BIOS to a 512 MB dynamic VRAM carveout** → host RAM jumps 3.5 GB → 14 GB
  and the GPU pulls weights from **GTT (system RAM)**. The hybrid then survives the exact failing
  workload with 5+ GB headroom, at **~62 tok/s warm (vs 65.8 carveout), 6/6** — near-free. Only
  caveat: **cold start is slow** (first request pages weights into GTT, then warms). Make this split
  the **standard BC-250 config**; it also frees the ~3 GB of VRAM the weights never used. The
  model-file mmap page cache (~2.9 GB `buff/cache`) is reclaimable and was never the culprit.

Safe relaunch:
```bash
podman rm -f dnc-bc250-q36
MODEL=$(readlink -f ~/models/qwen3.6-35b-reap-Q3_K_M.gguf)
podman run -d --name dnc-bc250-q36 --replace \
  --security-opt label=disable --device /dev/dri --group-add keep-groups \
  -p 8080:8080 -v "$MODEL":/models/model.gguf:ro \
  --entrypoint llama-server dnc/llamacpp-bc250:latest \
    -m /models/model.gguf -ngl 99 -c 16384 --parallel 1 \
    --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --temp 0.6 \
    --host 0.0.0.0 --port 8080 --alias qwen3.6-35b-reap
```
> Curl the node on **IPv4** (`127.0.0.1` / the node's A record) — `-p 8080:8080` publishes IPv4
> only; rootless podman resets the `::1` (IPv6) path, which looks like a crash but isn't.

## Tools referenced
- [`tools/gguf_dump.py`](../tools/gguf_dump.py) — dump metadata + per-block tensor map (stdlib, no `gguf` pkg).
- [`tools/gguf_set_uint.py`](../tools/gguf_set_uint.py) — in-place same-width uint metadata edit.
- [`tools/model_bench.py`](../tools/model_bench.py) — head-to-head coding benchmark.
