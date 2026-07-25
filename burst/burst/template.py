"""Purpose-built vLLM launch templates, generated from the model/GPU profile.

Templates are **computed, not hardcoded**, because the one setting that decides rental
economics — `--max-num-seqs` — depends on how much KV pool is left after the weights, which
depends on the model geometry, the quant, the context length and the card. A copy-pasted
`--max-num-seqs 2` wastes ~77% of a 96 GB card.

Two profiles:

* ``saturation`` (default) — maximise aggregate tokens/sec per dollar. Concurrency is set
  from the capacity plan. This is what a burst tier wants.
* ``interactivity`` — minimise per-stream latency. Correct for a single human at a keyboard,
  and the *worst case* for $/token. Never use it for a burst node.

Every switch is documented in ``SWITCHES`` and rendered into docs/burst-templates.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from .profiles import GPUS, MODELS, CapacityPlan, plan_capacity

#: Human-readable explanation of every switch we emit. Rendered into the markdown docs so
#: the reference and the generator can never drift.
SWITCHES: dict[str, str] = {
    "--served-model-name": "Name the gateway addresses. Keep it stable across quant changes "
        "so the LiteLLM entry doesn't need editing when you swap checkpoints.",
    "--max-model-len": "Context window. Directly multiplies KV cache: halving it doubles the "
        "sequences you can run. The single biggest lever after the model choice.",
    "--gpu-memory-utilization": "Fraction of VRAM vLLM may claim. 0.92 is a safe ceiling; "
        "above ~0.95 you risk OOM during a long prefill.",
    "--max-num-seqs": "Concurrent sequences. THE throughput/latency dial and the thing that "
        "decides rental economics. Computed here from the KV pool; do not copy a value from "
        "another card.",
    "--kv-cache-dtype": "fp8_e4m3 halves KV vs fp16 with negligible quality loss on Qwen3.6. "
        "Works on Ada+ and on SM120 for non-MLA models (Qwen dense/MoE qualify).",
    "--enable-prefix-caching": "Reuses the KV of a shared prompt prefix. The router's "
        "prefix-hash affinity deliberately keeps a stable system-prompt head, so this hits "
        "often — large win for agentic workloads.",
    "--enable-chunked-prefill": "Splits long prefills so decode isn't starved. Essential when "
        "concurrency is high and prompts are long.",
    "--long-prefill-token-threshold": "Prefills longer than this get chunked. Lower values "
        "smooth latency under concurrency at a small throughput cost.",
    "--enable-auto-tool-choice": "Lets the model emit tool calls unprompted. Required for "
        "agentic harnesses.",
    "--tool-call-parser": "Model-specific parser for tool-call syntax. Qwen3.5/3.6 use "
        "qwen3_coder. A mismatched parser yields malformed JSON tool calls.",
    "--reasoning-parser": "Separates chain-of-thought from the answer. Without it a reasoning "
        "model's thinking leaks into content and may never emit a stop token.",
    "--quantization": "Only needed when the checkpoint doesn't declare its own format. "
        "AutoRound checkpoints self-declare — passing it is redundant. ModelOpt NVFP4 needs "
        "modelopt_fp4.",
    "--speculative-config": "MTP speculative decoding — the model drafts its own next tokens. "
        "Up to ~2x throughput when draft acceptance is high, and a no-op (or worse) when it "
        "is not. Verify acceptance is non-zero before trusting it.",
    "--compilation-config": "cudagraph_mode=none works around cudaErrorStreamCaptureInvalidated "
        "on the MTP module on consumer Blackwell (SM120/SM121). Costs some performance; drop "
        "it once upstream fixes it.",
    "--api-key": "Bearer token the gateway presents. Always inject from an env var — never "
        "bake a key into a template.",
}

PROFILE_SATURATION = "saturation"
PROFILE_INTERACTIVITY = "interactivity"


@dataclass
class RenderedTemplate:
    model: str
    quant: str
    gpu: str
    profile: str
    image: str
    args: list[str]
    env: dict[str, str]
    plan: CapacityPlan
    warnings: list[str]

    @property
    def command(self) -> str:
        return "vllm serve " + " ".join(self.args)

    def as_dict(self) -> dict:
        return {
            "model": self.model, "quant": self.quant, "gpu": self.gpu,
            "profile": self.profile, "image": self.image,
            "args": self.args, "env": self.env, "warnings": self.warnings,
            "capacity": {
                "context": self.plan.context,
                "weights_gb": self.plan.weights_gb,
                "kv_pool_gib": self.plan.kv_pool_gib,
                "kv_bytes_per_token": self.plan.kv_bytes_per_token,
                "token_pool": self.plan.token_pool,
                "max_num_seqs": self.plan.max_num_seqs,
            },
        }


DEFAULT_IMAGE = "vllm/vllm-openai:latest"


def render(
    model_key: str, quant_key: str, gpu_key: str, *,
    context: int | None = None,
    profile: str = PROFILE_SATURATION,
    gpu_memory_utilization: float = 0.92,
    max_num_seqs: int | None = None,
    image: str = DEFAULT_IMAGE,
    api_key_env: str = "BURST_API_KEY",
    enable_mtp: bool = True,
) -> RenderedTemplate:
    """Build a launch template for this (model, quant, GPU) at the requested context."""
    if model_key not in MODELS:
        raise ValueError(f"unknown model {model_key!r} (have {sorted(MODELS)})")
    if gpu_key not in GPUS:
        raise ValueError(f"unknown gpu {gpu_key!r} (have {sorted(GPUS)})")
    model, gpu = MODELS[model_key], GPUS[gpu_key]

    plan = plan_capacity(gpu_key, model_key, quant_key, context=context,
                         gpu_memory_utilization=gpu_memory_utilization)
    warnings: list[str] = []
    if not plan.fits:
        raise ValueError(f"cannot serve: {plan.reason}")

    quant = model.quants[quant_key]

    # Concurrency: the whole point of the saturation profile.
    if max_num_seqs is not None:
        seqs = max_num_seqs
    elif profile == PROFILE_INTERACTIVITY:
        seqs = min(2, plan.max_num_seqs)
        warnings.append(
            "interactivity profile: --max-num-seqs is pinned low for per-stream latency. "
            "This is the WORST case for $/token — do not use it for a rented burst node.")
    else:
        seqs = plan.max_num_seqs
    if seqs > plan.max_num_seqs:
        warnings.append(
            f"--max-num-seqs {seqs} exceeds the {plan.max_num_seqs} the KV pool supports at "
            f"{plan.context:,} context; vLLM will queue or preempt.")

    args = [
        quant.hf_repo,
        "--served-model-name", model.name,
        "--max-model-len", str(plan.context),
        "--gpu-memory-utilization", str(gpu_memory_utilization),
        "--max-num-seqs", str(seqs),
        "--kv-cache-dtype", "fp8_e4m3",
        "--enable-prefix-caching",
        "--enable-chunked-prefill",
        "--long-prefill-token-threshold", "2048",
        "--enable-auto-tool-choice",
        "--tool-call-parser", model.tool_call_parser,
    ]
    if model.reasoning_parser:
        args += ["--reasoning-parser", model.reasoning_parser]
    if quant.vllm_quantization:
        args += ["--quantization", quant.vllm_quantization]

    # MTP speculative decoding — only where the checkpoint actually supports it.
    if enable_mtp and quant.supports_mtp:
        args += ["--speculative-config", '{"method":"mtp","num_speculative_tokens":3}']
        warnings.append(
            "MTP speculative decoding enabled. Qwen3.6 MTP support appears to be recent/"
            "nightly-only in vLLM — verify draft acceptance is non-zero, or it is dead weight.")
        if gpu.arch == "blackwell":
            args += ["--compilation-config", '{"cudagraph_mode":"none"}']
            warnings.append(
                "SM120 + MTP: cudagraph_mode=none is currently required "
                "(cudaErrorStreamCaptureInvalidated). Remove once fixed upstream.")
    elif enable_mtp and not quant.supports_mtp:
        warnings.append(
            f"MTP not enabled: the {quant.name} checkpoint does not preserve a usable MTP "
            f"head. A vanilla quant packs mtp.fc at low precision and draft acceptance "
            f"silently drops to 0%.")

    args += ["--api-key", f"${api_key_env}"]

    env = {
        "VLLM_USE_FLASHINFER_SAMPLER": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True,max_split_size_mb:512",
    }
    if gpu.arch == "blackwell":
        env["TORCH_CUDA_ARCH_LIST"] = "12.0"

    # Known-bad combinations worth refusing loudly rather than debugging at $1.69/hr.
    if model.is_moe and quant_key == "nvfp4" and gpu.arch == "blackwell":
        raise ValueError(
            "NVFP4 MoE on SM120 is unsupported in vLLM (issue #35065 closed as not planned; "
            "#31085 still open). Use the autoround quant for MoE models on Blackwell.")
    if gpu.arch == "ampere" and quant_key in ("nvfp4", "fp8"):
        raise ValueError(
            f"{quant_key} needs FP4/FP8 tensor cores; {gpu.name} is Ampere. "
            f"Use autoround (INT4/Marlin) on Ampere.")
    if gpu.vram_gb <= 24 and not model.is_moe:
        warnings.append(
            "Single 24GB card + dense model: vLLM has a documented prefill cliff "
            "(OOM above ~50k single-prompt, multi-turn failures ~25k accumulated) that makes "
            "it unsafe for agentic harnesses. Prefer tensor-parallel across 2 cards, an MoE "
            "model, or llama.cpp/ik_llama.")

    return RenderedTemplate(model.name, quant.name, gpu_key, profile, image, args, env,
                            plan, warnings)


def best_template(
    model_key: str = None, *, context: int = 262144, min_seqs: int = 8,
    prefer_quant: tuple[str, ...] = ("nvfp4", "autoround"),
) -> RenderedTemplate:
    """Cheapest card that serves this model at `context` with at least `min_seqs`
    concurrency — the default choice for a saturation tier."""
    from .profiles import DEFAULT_MODEL, rank_gpus
    model_key = model_key or DEFAULT_MODEL
    for quant in prefer_quant:
        if quant not in MODELS[model_key].quants:
            continue
        for plan in rank_gpus(model_key, quant, context=context, min_seqs=min_seqs):
            try:
                return render(model_key, quant, plan.gpu, context=context)
            except ValueError:
                continue
    raise ValueError(
        f"no GPU serves {model_key} at {context:,} context with >={min_seqs} concurrency")
