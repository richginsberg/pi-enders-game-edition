# Model selection — S2 / S3 coding models (2026-07)

Research for task #20: pick recent, coding-strong models that fit the fleet's fixed VRAM
budgets. Numbers verified via web research (July 2026) + KV math from model configs.

## VRAM budget method
`fit = weights(quant) + KV(layers, ctx, kv_heads, head_dim, kv_quant) + compute (~0.5–1.5 GB)`.
KV per token per full-attention layer = `2 (K+V) × kv_heads × head_dim × bytes/elem`
(q8_0 ≈ 1.06 B/elem, q4 ≈ 0.56, f16 = 2). **Only full-attention layers grow KV** — this
matters enormously for the Qwen3.6 hybrid models below.

---

## S3 — BC-250, **12 GB** VRAM (weights + KV must fit ~11 GB usable)

Constraint forces Q4 for anything ≥14B. Best coding models that fit:

| Model | Quant | Weights | Context (q8 KV) | Notes |
|---|---|---|---|---|
| **Qwen3-Coder-30B-A3B (REAP→15B)** *(current)* | Q4_K_M | 8.9 GB | ~65k | MoE ~3B active; **measured 80 tok/s, 6/6 on the coding suite**; Qwen3 gen |
| **Qwen3.6-35B-REAP→19B** (RangerX ratio-0.5) | Q4_K_S | ~10.7 GB | ~16–32k | **candidate.** Qwen3.6 gen (newer/better coding), MoE ~3B active, hybrid attn (10/40 full-attn, 2 KV heads → tiny KV); tight at Q4 |
| " | Q3_K_M | ~9.3 GB | ~128k+ | same model, trade quality for long context |
| **Qwen3-Coder-14B** | Q4_K_M | ~9.5 GB | ~24–32k | dense; top 12 GB dense coder in 2026 reviews |

**The live shootout (task #20):** current vs the RangerX Qwen3.6-35B-REAP. The new model is a
**newer generation at 19B** (likely better coding) but bigger → tighter context at Q4. KV is
almost free on it (hybrid: only 10 of 40 layers grow KV, 2 KV heads → ~2.85 GB @ 262k), so
it's **weights-bound**: Q4_K_S fits ~32k ctx, Q3_K_M fits ~128k+. Decide by benchmark:
```
# after quantizing RangerX -> GGUF and loading it in the BC-250 container:
python3 tools/model_bench.py --base http://<node>:8080/v1 --model <alias> --tag qwen36-reap --run-code
python3 tools/model_bench.py --compare qwen3coder-current qwen36-reap
```
Baseline captured: **qwen3coder-current = 80 tok/s, 6/6**.

**VERDICT (2026-07-04, final after BIOS fix): RangerX Qwen3.6-35B-REAP IS viable at S3 with the
dynamic-VRAM BIOS split. Choose vs Qwen3-Coder on quality (Terminal-Bench), not fit.**

The story in three acts:
1. **Toy suite tied** 6/6 vs 6/6 (RangerX 66 tok/s, −18%) → can't separate them on quality; needs
   a real bench.
2. **Fixed 12 GB carveout OOM'd the hybrid.** On the default BIOS split (12 GB VRAM + only 3.5 GB
   host), Qwen3.6's hybrid host-side buffers (`kv_unified='false'` per-slot KV + Gated-DeltaNet
   **SSM recurrent state**, scaling with `slots × ctx`) blew the 3.5 GB host under 4-slot
   concurrency → `Exited (137)` (host `global_oom`; VRAM was fine). A/B confirmed **hybrid-specific,
   not tier-wide**: the current Qwen3-Coder-30B-REAP (plain qwen3moe, `kv_unified='true'`, no SSM)
   held rock-stable at the identical failing config.
3. **Dynamic 512 MB VRAM split fixed it.** Re-partitioning the BIOS to a 512 MB dynamic VRAM
   carveout flips host RAM **3.5 GB → 14 GB**; the GPU pulls weights from **GTT (system RAM)**. The
   hybrid now survives the exact failing workload with **5+ GB host headroom**, at **62 tok/s gen
   (vs 65.8 carveout, ~6%), prefill higher, 6/6**. Only caveat: **cold start is slow** (first
   request pages weights into GTT), then warms up.

**Recommendations:** (a) **make the 512 MB dynamic-VRAM split the standard BC-250 config fleet-wide**
— near-free throughput, unlocks the full 14 GB host, no more concurrency OOM. (b) S3 model is now a
genuine **speed vs quality** call: current Qwen3-Coder (80 tok/s, faster) vs Qwen3.6-35B (62 tok/s,
newer gen, now fits) — **resolve via Terminal-Bench**, which is finally runnable on the node since
concurrency no longer OOMs. See [qwen36-gguf-conversion.md](qwen36-gguf-conversion.md) §6 and the
[BC-250 README](../deploy/bc250/README.md).

---

## S2 — V100 / MI50 / RTX 5090, **32 GB** VRAM → **Qwen3.6-27B @ 262k**

**This works — and the hybrid architecture is why.** Qwen3.6-27B (dense, Apr 2026, Apache
2.0, 262k native / 1M extensible) uses Gated DeltaNet hybrid attention: **16 full-attention
layers + 48 linear** (of 64), GQA with **4 KV heads, head_dim 256**. KV grows with only the
16 full layers:

KV @ 262k = `16 × 262144 × 2 × 4 × 256 × bytes` ≈ **9.1 GB (q8)** / 4.8 GB (q4) / 17 GB (f16).
(A normal 64-layer 27B would be ~27 GB of KV at 262k — infeasible on 32 GB. The linear layers
are what make this fit.)

Fit on 32 GB (weights from unsloth: 4-bit 18 GB, 6-bit 24 GB, 8-bit 30 GB):

| Weights | KV @262k | + compute | Total | Fits 32 GB? |
|---|---|---|---|---|
| **4-bit** (18 GB) | **q8 (9.1)** | ~1.5 | **~28.6 GB** | ✅ full 262k, headroom |
| 6-bit (24 GB) | q4 (4.8) | ~1.5 | ~30.3 GB | ✅ tight |
| 8-bit (30 GB) | — | — | >32 | ❌ no room for long-context KV |

**Recommendation:** **Qwen3.6-27B at 4-bit (AWQ or Q4_K_M) + q8 KV → full 262k context ≈
28–29 GB, fits all three 32 GB cards.** Coding is flagship-class (Qwen claims Terminal-Bench
2.0 parity with Claude 4.5 Opus — verify on an independent leaderboard, see below).

**Serving engine per card** (the cards differ a lot):
- **RTX 5090 (32 GB, Blackwell sm120)** — *preferred S2 host.* vLLM has Qwen3.6 recipes and
  supports the Gated DeltaNet kernels; best prefill/throughput for long context. Use vLLM +
  AWQ 4-bit @ 262k.
- **V100 (32 GB, Volta sm70)** — newest vLLM is dropping Volta and the hybrid kernels may not
  exist for sm70; use **llama.cpp (GGUF Q4)**. Long context works, slower prefill.
- **MI50 (32 GB, gfx906)** — no vLLM (gfx906 unsupported); **llama.cpp ROCm/Vulkan (GGUF Q4)**.
  Slowest; fine as a spillover S2 node.

**Alternative S2 model:** **Qwen3.6-35B-A3B** (MoE, ~3B active, same hybrid attention, Apr
2026) — faster generation than the 27B dense, similar footprint at Q4 (~18–20 GB + ~9 GB KV);
good if S2 throughput matters more than the dense model's quality ceiling.

---

## Terminal-Bench landscape (2026-07, for tier calibration)
Independent leaderboards (llmrun / pricepertoken) put **GLM-5 ~52%** as the top *open* model,
**MiniMax M2.x** close behind; frontier closed (Fable 5 ~63%, Opus 4.8 ~58%) leads overall.
Qwen3.6-27B's Terminal-Bench claim is from Qwen's own blog — **cross-check on an independent
leaderboard before trusting the exact number.** For our tiers this confirms: S0 = GLM/xAI
(frontier), S2 = Qwen3.6-27B (strong local), S3 = Qwen3-Coder small (good-enough cheap/wide).

## Sources
- [Terminal-Bench leaderboard (llmrun)](https://llmrun.dev/benchmark/terminalbench) · [pricepertoken](https://pricepertoken.com/leaderboards/benchmark/terminalbench)
- [Qwen3.6-27B model card (HF)](https://huggingface.co/Qwen/Qwen3.6-27B) · [Unsloth run guide](https://unsloth.ai/docs/models/qwen3.6) · [QwenLM/Qwen3.6 (GitHub)](https://github.com/QwenLM/Qwen3.6)
- [Best coding LLM for 12GB (2026)](https://www.promptquorum.com/prompt-bites/best-local-llm-coding-12gb-vram) · [Best local coding LLMs 2026](https://runaihome.com/blog/best-local-coding-llm-2026/)
- [Deploy Qwen 3.5/3.6 hybrid + vLLM](https://www.spheron.network/blog/deploy-qwen-3-5-gpu-cloud/)
