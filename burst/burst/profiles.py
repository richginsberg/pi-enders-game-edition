"""Model / quant / GPU profiles and the capacity math derived from them.

Everything here is grounded in values read off real `config.json` files and vendor pages
(dated in the table below) — not estimates — because the concurrency planner multiplies them
out and a wrong constant silently produces an unservable config.

The headline insight: both Qwen3.6 models use **hybrid linear attention** (a repeating
3-linear : 1-full layer pattern), so only a minority of layers hold a growing KV cache. That
makes long context far cheaper than the parameter count suggests, and it is why a burst node
can run high concurrency at full 262k context.

    Qwen3.6-27B      64 layers, 16 full-attn, 4 kv-heads, head_dim 256 -> 32 KiB/token @fp8
    Qwen3.6-35B-A3B  40 layers, 10 full-attn, 2 kv-heads, head_dim 256 -> 10 KiB/token @fp8

The MoE is 3.2x cheaper per token of KV *and* activates only 3B params, which is why it is
the default for a saturation tier even though it scores slightly lower on SWE-bench.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GIB = 1024 ** 3


@dataclass(frozen=True)
class QuantProfile:
    """A specific quantized checkpoint of a model."""
    name: str
    hf_repo: str
    weights_gb: float          # measured/published on-disk+VRAM weight size
    vllm_quantization: str | None  # explicit --quantization value, or None to autodetect
    min_arch: str              # "ampere" | "ada" | "blackwell" — minimum GPU arch
    supports_mtp: bool = False # speculative decoding via the model's MTP head
    notes: str = ""


@dataclass(frozen=True)
class ModelProfile:
    """A servable model, with the attention geometry needed to size its KV cache."""
    name: str
    hf_base: str
    total_params_b: float
    active_params_b: float     # == total for dense; small for MoE
    is_moe: bool
    layers: int
    full_attn_layers: int      # only these hold a growing KV cache (hybrid linear attention)
    kv_heads: int
    head_dim: int
    max_context: int
    swe_bench_verified: float | None   # published, comparable across candidates
    tool_call_parser: str
    reasoning_parser: str | None
    quants: dict[str, QuantProfile] = field(default_factory=dict)

    def kv_bytes_per_token(self, kv_dtype_bytes: int = 1) -> int:
        """KV cache cost of one token. fp8 -> 1 byte/element, fp16 -> 2."""
        return 2 * self.kv_heads * self.head_dim * self.full_attn_layers * kv_dtype_bytes


@dataclass(frozen=True)
class GpuSpec:
    name: str
    vram_gb: float
    arch: str                  # ampere | ada | blackwell
    usd_per_hr: float          # provider list rate (RunPod Community unless noted)
    notes: str = ""


# --- GPUs (RunPod Community Cloud, verified 2026-07-25) -----------------------------
GPUS: dict[str, GpuSpec] = {
    "rtx3090":   GpuSpec("RTX 3090", 24, "ampere", 0.22,
                         "no FP8/FP4 tensor cores; single-card vLLM unsafe for agentic prefill"),
    "rtxa5000":  GpuSpec("RTX A5000", 24, "ampere", 0.16, "best VRAM per $/hr when in stock"),
    "rtx4090":   GpuSpec("RTX 4090", 24, "ada", 0.34, ""),
    "l40":       GpuSpec("L40", 48, "ada", 0.69, ""),
    "rtx6000ada": GpuSpec("RTX 6000 Ada", 48, "ada", 0.74, ""),
    "a100pcie":  GpuSpec("A100 PCIe", 80, "ampere", 1.19, "Ampere: no FP4"),
    "rtx5090":   GpuSpec("RTX 5090", 32, "blackwell", 0.69, "SM120"),
    "rtxpro6000": GpuSpec("RTX PRO 6000 WK", 96, "blackwell", 1.69, "SM120"),
    "h100nvl":   GpuSpec("H100 NVL", 94, "hopper", 2.59, "FP8 yes, FP4 no"),
}

_ARCH_RANK = {"ampere": 0, "ada": 1, "hopper": 2, "blackwell": 3}


# --- Models (geometry from each repo's config.json, fetched 2026-07-25) -------------
QWEN36_27B = ModelProfile(
    name="qwen3.6-27b",
    hf_base="Qwen/Qwen3.6-27B",
    total_params_b=27, active_params_b=27, is_moe=False,
    layers=64, full_attn_layers=16, kv_heads=4, head_dim=256,
    max_context=262144, swe_bench_verified=77.2,
    tool_call_parser="qwen3_coder", reasoning_parser="qwen3",
    quants={
        "autoround": QuantProfile(
            "autoround", "Lorbus/Qwen3.6-27B-int4-AutoRound", 18.0, None, "ampere",
            supports_mtp=True,
            notes="INT4 W4A16. MTP head kept BF16 — a vanilla auto-round packs mtp.fc as "
                  "INT4, vLLM's loader skips it, and draft acceptance silently drops to 0%."),
        "nvfp4": QuantProfile(
            "nvfp4", "nvidia/Qwen3.6-27B-NVFP4", 15.0, "modelopt_fp4", "blackwell",
            supports_mtp=False,
            notes="W4A4 — real 4-bit compute, ~1.9-2.1x BF16 measured on RTX PRO 6000. "
                  "Blackwell only. Slightly LARGER than INT4; the win is FLOPs, not bytes."),
        "fp8": QuantProfile(
            "fp8", "Qwen/Qwen3.6-27B-FP8", 28.0, None, "ada",
            notes="Official FP8. Needs Ada+ for native FP8 tensor cores."),
    },
)

QWEN36_35B_A3B = ModelProfile(
    name="qwen3.6-35b-a3b",
    hf_base="Qwen/Qwen3.6-35B-A3B",
    total_params_b=35, active_params_b=3, is_moe=True,
    layers=40, full_attn_layers=10, kv_heads=2, head_dim=256,
    max_context=262144, swe_bench_verified=73.4,
    tool_call_parser="qwen3_coder", reasoning_parser="qwen3",
    quants={
        "autoround": QuantProfile(
            "autoround", "Intel/Qwen3.6-35B-A3B-int4-AutoRound", 20.0, None, "ampere",
            notes="Size is ESTIMATED (~4-bit arithmetic); no published per-quant figure. "
                  "Verify before sizing a card exactly to it."),
        # NVFP4 deliberately absent: MoE NVFP4 on SM120 is unsupported in vLLM
        # (issue #35065 closed as not planned; #31085 still a feature request).
    },
)

MODELS: dict[str, ModelProfile] = {m.name: m for m in (QWEN36_27B, QWEN36_35B_A3B)}

#: Default for a saturation tier: 3B active + 10 KiB/token KV batches far better than the
#: dense 27B, and still scores +11 SWE-bench over gpt-oss-120b.
DEFAULT_MODEL = "qwen3.6-35b-a3b"


# --- capacity math ------------------------------------------------------------------
@dataclass(frozen=True)
class CapacityPlan:
    gpu: str
    model: str
    quant: str
    context: int
    kv_dtype_bytes: int
    weights_gb: float
    kv_pool_gib: float
    kv_bytes_per_token: int
    token_pool: int
    max_num_seqs: int
    fits: bool
    reason: str = ""


def arch_ok(gpu: GpuSpec, quant: QuantProfile) -> bool:
    return _ARCH_RANK[gpu.arch] >= _ARCH_RANK[quant.min_arch]


def plan_capacity(
    gpu_key: str, model_key: str, quant_key: str, *,
    context: int | None = None, kv_dtype_bytes: int = 1,
    gpu_memory_utilization: float = 0.92, headroom_gb: float = 0.0,
) -> CapacityPlan:
    """Size a deployment: how much KV pool is left after weights, and therefore how many
    concurrent sequences of `context` tokens fit. This is what turns "which card" into a
    concrete `--max-num-seqs`, instead of guessing (a guess of 2 wastes ~77% of a 96GB card).
    """
    gpu, model = GPUS[gpu_key], MODELS[model_key]
    if quant_key not in model.quants:
        return CapacityPlan(gpu_key, model_key, quant_key, context or model.max_context,
                            kv_dtype_bytes, 0, 0, 0, 0, 0, False,
                            f"{model.name} has no '{quant_key}' quant "
                            f"(have: {sorted(model.quants)})")
    quant = model.quants[quant_key]
    ctx = min(context or model.max_context, model.max_context)

    if not arch_ok(gpu, quant):
        return CapacityPlan(gpu_key, model_key, quant_key, ctx, kv_dtype_bytes,
                            quant.weights_gb, 0, 0, 0, 0, False,
                            f"{quant.name} needs {quant.min_arch}+, {gpu.name} is {gpu.arch}")

    kv_tok = model.kv_bytes_per_token(kv_dtype_bytes)
    pool_gib = gpu.vram_gb * gpu_memory_utilization - quant.weights_gb - headroom_gb
    if pool_gib <= 0:
        return CapacityPlan(gpu_key, model_key, quant_key, ctx, kv_dtype_bytes,
                            quant.weights_gb, 0, kv_tok, 0, 0, False,
                            f"weights {quant.weights_gb}GB exceed usable VRAM on {gpu.name}")

    token_pool = int(pool_gib * GIB / kv_tok)
    seqs = token_pool // ctx
    if seqs < 1:
        return CapacityPlan(gpu_key, model_key, quant_key, ctx, kv_dtype_bytes,
                            quant.weights_gb, pool_gib, kv_tok, token_pool, 0, False,
                            f"KV pool holds {token_pool:,} tokens — under one {ctx:,}-token "
                            f"sequence. Lower --max-model-len or use a bigger card.")
    return CapacityPlan(gpu_key, model_key, quant_key, ctx, kv_dtype_bytes,
                        quant.weights_gb, round(pool_gib, 2), kv_tok, token_pool,
                        int(seqs), True)


def rank_gpus(
    model_key: str, quant_key: str, *, context: int, min_seqs: int = 8,
    kv_dtype_bytes: int = 1,
) -> list[CapacityPlan]:
    """Every GPU that can serve this model/quant at `context` with >= min_seqs concurrency,
    cheapest first. The saturation tier wants the cheapest card that still batches."""
    out = []
    for key in GPUS:
        plan = plan_capacity(key, model_key, quant_key, context=context,
                             kv_dtype_bytes=kv_dtype_bytes)
        if plan.fits and plan.max_num_seqs >= min_seqs:
            out.append(plan)
    return sorted(out, key=lambda p: GPUS[p.gpu].usd_per_hr)
