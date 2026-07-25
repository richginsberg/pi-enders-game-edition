# Burst templates — every switch, and how the model changes it

Launch templates for a rented burst node are **generated, not copy-pasted**
([`burst/burst/template.py`](../burst/burst/template.py)), because the setting that decides
rental economics — `--max-num-seqs` — depends on how much KV cache is left after the weights,
which depends on the model geometry, the quant, the context length *and* the card.

```bash
dnc-burst template --model qwen3.6-35b-a3b --quant autoround --gpu l40
dnc-burst switches          # what every flag does
dnc-burst capacity --model qwen3.6-35b-a3b --context 262144 --min-seqs 8
```

> Model geometry below is read from each repo's `config.json`; GPU rates are RunPod
> Community, verified 2026-07-25.

## The two profiles

| | `saturation` (default) | `interactivity` |
|---|---|---|
| Optimises | aggregate tokens/sec **per dollar** | per-stream latency |
| `--max-num-seqs` | computed from the KV pool | pinned to 2 |
| Correct for | a rented burst node | one human at a keyboard |

A rented GPU bills wall-clock, so the only thing that matters is total tokens produced per
paid hour. `--max-num-seqs 2` on a 96 GB card uses **23% of the KV pool** and multiplies your
$/token by roughly 4. The generator refuses to leave that on the table silently — the
`interactivity` profile emits a warning saying so.

## Why the model choice dominates everything

Both Qwen3.6 models use **hybrid linear attention** — a repeating 3-linear : 1-full layer
pattern — so only a minority of layers hold a growing KV cache:

| | Qwen3.6-27B | Qwen3.6-35B-A3B |
|---|---|---|
| Type | dense, **27B active** | MoE, **3B active** |
| Layers / full-attention | 64 / **16** | 40 / **10** |
| KV heads × head_dim | 4 × 256 | **2** × 256 |
| **KV per token @fp8** | **32 KiB** | **10 KiB** ← 3.2× cheaper |
| Full 262k context KV | 8.0 GiB | 2.5 GiB |
| ~4-bit weights | 18 GB | ~20 GB *(estimated)* |
| SWE-bench Verified | **77.2** | 73.4 |

`KV bytes/token = 2 × kv_heads × head_dim × full_attn_layers × dtype_bytes`

**Consequence:** the MoE is the saturation model. It activates 3B params (fast) *and* costs
3.2× less KV per token, so the same card runs far more concurrent sequences. The dense 27B
buys +3.8 SWE-bench points at a large throughput cost. Pick the 27B when quality per request
matters; pick the 35B-A3B when tokens-per-dollar matters — which, for a rented node, is
almost always.

Both still beat `gpt-oss-120b` (62.4) by a wide margin.

## How the card changes it

`dnc-burst capacity --model qwen3.6-35b-a3b --context 262144 --min-seqs 8`

| GPU | $/hr | VRAM | max-num-seqs @262k | KV pool |
|---|---|---|---|---|
| L40 | 0.69 | 48 GB | 9 | 24.2 GiB |
| RTX 6000 Ada | 0.74 | 48 GB | 9 | 24.2 GiB |
| A100 PCIe | 1.19 | 80 GB | 21 | 53.6 GiB |
| RTX PRO 6000 | 1.69 | 96 GB | **27** | 68.3 GiB |
| H100 NVL | 2.59 | 94 GB | 26 | 66.5 GiB |

The dense 27B on the same cards gets roughly a third of that concurrency. And **halving
`--max-model-len` doubles the sequences** — context is the second-biggest lever after the
model.

## Quantization by GPU generation

| Arch | Cards | Use | Why |
|---|---|---|---|
| Ampere SM86 | RTX 3090, A5000, A100 | **AutoRound/AWQ INT4** (Marlin) | no FP8 *or* FP4 tensor cores |
| Ada SM89 | RTX 4090, L40, 6000 Ada | AutoRound INT4 or FP8 | FP8 yes, FP4 no |
| Blackwell SM120 | RTX 5090, PRO 6000 | **NVFP4** (dense only) | native 4-bit tensor cores |

NVFP4 is **W4A4** — weights *and* activations in 4-bit, so it buys real FLOPs (measured
1.9–2.1× BF16 on a PRO 6000). AutoRound/AWQ are W4A16: memory bandwidth only. NVFP4 is
~15 GB vs ~14 GB for INT4 — slightly *larger*; the win is compute, not bytes.

**Combinations the generator refuses** (loudly, rather than letting you debug at $1.69/hr):

- **NVFP4 + MoE + SM120** — unsupported in vLLM (issue #35065 closed as not planned, #31085
  still open). Use `autoround` for MoE on Blackwell.
- **NVFP4 or FP8 on Ampere** — no such tensor cores.
- **Any config whose KV pool can't hold one full sequence** — you'd get an unservable node.

And one it warns about: **a single 24 GB card with a dense model.** vLLM has a documented
prefill cliff (OOM above ~50k single-prompt, multi-turn failures ~25k accumulated) that makes
it unsafe for agentic harnesses. Use TP=2, an MoE, or llama.cpp/ik_llama.

## Every switch

| Switch | What it does |
|---|---|
| `--served-model-name` | Name the gateway addresses. Keep it stable across quant changes so the LiteLLM entry needn't be edited when you swap checkpoints. |
| `--max-model-len` | Context window. Directly multiplies KV cache: halving it doubles the sequences you can run. |
| `--gpu-memory-utilization` | Fraction of VRAM vLLM may claim. 0.92 is a safe ceiling; above ~0.95 you risk OOM during a long prefill. |
| `--max-num-seqs` | **The** throughput/latency dial and the thing that decides rental economics. Computed from the KV pool — never copy it from another card. |
| `--kv-cache-dtype fp8_e4m3` | Halves KV vs fp16 with negligible quality loss on Qwen3.6. Fine on Ada+ and on SM120 for non-MLA models (Qwen qualifies; the Blackwell FP8-KV blocker is MLA-only). |
| `--enable-prefix-caching` | Reuses the KV of a shared prompt prefix. The router's prefix-hash affinity keeps a stable system-prompt head, so this hits often. |
| `--enable-chunked-prefill` | Splits long prefills so decode isn't starved. Essential at high concurrency with long prompts. |
| `--long-prefill-token-threshold` | Prefills longer than this get chunked. Lower smooths latency under load at a small throughput cost. |
| `--enable-auto-tool-choice` | Lets the model emit tool calls unprompted. Required for agentic harnesses. |
| `--tool-call-parser qwen3_coder` | Model-specific tool-call syntax parser. A mismatch yields malformed JSON tool calls. |
| `--reasoning-parser qwen3` | Separates chain-of-thought from the answer. Without it, thinking leaks into content and the model may never emit a stop token. |
| `--quantization` | Only needed when the checkpoint doesn't declare its own format. AutoRound self-declares (passing it is redundant); ModelOpt NVFP4 needs `modelopt_fp4`. |
| `--speculative-config` | MTP speculative decoding — the model drafts its own next tokens. Up to ~2× when acceptance is high, dead weight when it isn't. |
| `--compilation-config` | `cudagraph_mode=none` works around `cudaErrorStreamCaptureInvalidated` on the MTP module on consumer Blackwell. Costs performance; drop it when fixed upstream. |
| `--api-key` | Always injected from an env var. The generator asserts no literal key is ever baked in. |

Env vars emitted: `VLLM_USE_FLASHINFER_SAMPLER=1`,
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:512`, plus
`TORCH_CUDA_ARCH_LIST=12.0` on Blackwell (stock images fail on SM120 without it).

## The MTP trap

MTP speculative decoding is only enabled when the **checkpoint preserves a usable MTP head**.
A vanilla AutoRound run packs `mtp.fc` at INT4; vLLM's `Qwen3_5MTP` loader then skips it
(`fc.qweight` vs the expected `fc.weight`) and **draft acceptance silently drops to 0%** —
you pay the speculative overhead and get nothing. `Lorbus/Qwen3.6-27B-int4-AutoRound`
dequantizes that head back to BF16 specifically to fix this.

Two caveats the generator warns about:
- Qwen3.6 MTP support looks **recent/nightly-only** in vLLM. **Verify draft acceptance is
  non-zero** before believing any speedup.
- On SM120, MTP needs `cudagraph_mode=none`.

## Worked examples

**Saturation default — MoE on the cheapest card that batches ($0.69/hr):**
```bash
vllm serve Intel/Qwen3.6-35B-A3B-int4-AutoRound \
  --served-model-name qwen3.6-35b-a3b --max-model-len 262144 \
  --gpu-memory-utilization 0.92 --max-num-seqs 9 --kv-cache-dtype fp8_e4m3 \
  --enable-prefix-caching --enable-chunked-prefill --long-prefill-token-threshold 2048 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 \
  --api-key $BURST_API_KEY
# KV 10 KiB/tok · pool 24.2 GiB · 2,533,359 tokens · 9 concurrent @262k
```

**Max quality — dense 27B in NVFP4 on Blackwell ($1.69/hr):**
```bash
export TORCH_CUDA_ARCH_LIST=12.0
vllm serve nvidia/Qwen3.6-27B-NVFP4 \
  --served-model-name qwen3.6-27b --max-model-len 262144 \
  --gpu-memory-utilization 0.92 --max-num-seqs 9 --kv-cache-dtype fp8_e4m3 \
  --enable-prefix-caching --enable-chunked-prefill --long-prefill-token-threshold 2048 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 \
  --quantization modelopt_fp4 --api-key $BURST_API_KEY
# NVFP4 has no MTP head -> speculative decoding is correctly omitted
```

**Budget — dense 27B on a 24 GB Ampere card, reduced context ($0.22/hr):**
```bash
vllm serve Lorbus/Qwen3.6-27B-int4-AutoRound \
  --served-model-name qwen3.6-27b --max-model-len 32768 \
  --gpu-memory-utilization 0.92 --max-num-seqs 4 --kv-cache-dtype fp8_e4m3 \
  ... --speculative-config '{"method":"mtp","num_speculative_tokens":3}' \
  --api-key $BURST_API_KEY
# KV pool only 4.08 GiB -> 133,693 tokens -> 4 seqs @32k. Full 262k does NOT fit.
# Also warns: single 24GB + dense hits the vLLM prefill cliff. Prefer TP=2 or the MoE.
```

## Changing model → what to re-check

1. **KV per token** — recompute `2 × kv_heads × head_dim × full_attn_layers × dtype_bytes`
   from the new `config.json`. This changes `--max-num-seqs` on every card.
2. **Weight size** — changes the KV pool, hence concurrency again.
3. **Parsers** — `--tool-call-parser` / `--reasoning-parser` are model-family specific.
4. **MTP** — does the checkpoint keep a usable MTP head? If not, drop `--speculative-config`.
5. **Quant support** — MoE + NVFP4 is unsupported on SM120; check the arch gate.
6. **Re-run `dnc-burst capacity`** rather than reusing the old card choice.

Add a model by appending a `ModelProfile` in
[`burst/burst/profiles.py`](../burst/burst/profiles.py) — every template, capacity plan and
guardrail derives from it.
