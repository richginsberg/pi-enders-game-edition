# Burst GPU — renting compute by the hour

**Status: designed, not built.** Plan for provisioning a rented GPU for a long-running task
(hours), registering it into the gateway as a tier, and releasing it when done — reusing the
registry → `litellm-sync` plumbing the physical nodes already use.

> Prices/behaviour verified **2026-07-25** from live RunPod, vLLM and HuggingFace pages.
> Marked UNVERIFIED where we could not read it off a page. Re-check before committing spend.

## TL;DR — three findings that should shape the build

1. **"Stop the pod, pay only for disk" does not work the way it sounds.** Stopping
   *doubles* the volume-disk rate ($0.10 → **$0.20/GB/mo**), wipes the container disk,
   releases the GPU with **no reservation**, and a restart can silently hand you a
   **zero-GPU pod** that still bills storage. Use **network volume + terminate** instead.
2. **Which card you rent decides which tier you can undercut.** A $0.22/hr RTX 3090
   saturated with a small model beats the cheap API tiers. A $1.69/hr RTX Pro 6000 never
   can — at *any* throughput — but it comfortably displaces S0/S1.
3. **Don't run GGUF under vLLM.** It's out-of-tree and "highly experimental and
   under-optimized". You don't need to build a quant either — good ones already exist.

## Economics

Break-even is `sustained tok/s = hourly_rate ÷ price_per_output_token`. Below that rate the
hosted API is cheaper, because rental bills wall-clock, not tokens.

### Break-even sustained tok/s, by card and target tier

RunPod **Community Cloud** rates.

| Target tier ($/M out) | 3080 Ti 12GB $0.18 | 3090 24GB $0.22 | 5090 32GB $0.69 | L40 48GB $0.69 | A100 80GB $1.19 | Pro 6000 96GB $1.69 |
|---|---|---|---|---|---|---|
| S0 `opus-5` 25.00 | 2 | 2 | 8 | 8 | 13 | **19** |
| S1 `sonnet-5` 10.00 | 5 | 6 | 19 | 19 | 33 | **47** |
| S1 `deepseek-v4-pro` 0.87 | 57 | 70 | 220 | 220 | 380 | 540 |
| S2 `deepseek-v4-flash` 0.188 | 266 | 325 | 1,020 | 1,020 | 1,758 | 2,497 |
| S3 `gpt-oss-120b` 0.170 | **294** | **359** | 1,127 | 1,127 | 1,944 | 2,761 |

### The two plays

**Play A — big card, displace the frontier.** RTX Pro 6000 96GB @ $1.69/hr running
Qwen3.6-27B-int4 at ~100 tok/s (`--max-num-seqs 2`) = **$4.69 per 1M output tokens**:

| vs | verdict |
|---|---|
| S0 `opus-5` $25/M | **rental 5.3× cheaper** ✅ |
| S1 `sonnet-5` $10/M | **rental 2.1× cheaper** ✅ |
| S1 `deepseek-v4-pro` $0.87/M | API 5.4× cheaper ❌ |
| S2 `deepseek-v4-flash` $0.188/M | API 25× cheaper ❌ |
| S3 `gpt-oss-120b` $0.17/M | API 27.6× cheaper ❌ |

This card **cannot reach S2/S3 pricing at any achievable throughput** — even a fantasy
1,600 tok/s lands at $0.29/M vs $0.17. Its job is replacing *closed frontier models* with an
open one you control.

**Play B — cheap card, undercut the bulk tiers.** RTX 3090 24GB @ $0.22/hr:

| sustained | $/M out | beats S3 $0.17? |
|---|---|---|
| 100 tok/s | $0.611 | no |
| 200 tok/s | $0.306 | no |
| **400 tok/s** | **$0.153** | **yes** |

400 tok/s aggregate on a 24 GB card needs a small model and **high `--max-num-seqs`** — i.e.
a fan-out workload, not interactive chat. That's the only configuration where renting beats
the cheap hosted tiers.

**Note on `--max-num-seqs`:** it is the single biggest lever on rental economics and it
trades directly against latency. `--max-num-seqs 2` (+ `--performance-mode interactivity`)
optimises per-stream responsiveness and is the *worst case* for $/token. For batch/fan-out
work, raise it — aggregate throughput scales roughly with it until VRAM or compute saturates.

### Value per dollar

Best VRAM-per-$/hr on Community (a proxy for "what can I fit cheaply"):

| GPU | VRAM | $/hr | GB per $/hr |
|---|---|---|---|
| RTX A5000 | 24 GB | $0.16 | **150** |
| RTX 3090 | 24 GB | $0.22 | 109 |
| RTX A4500 | 20 GB | $0.19 | 105 |
| RTX A4000 | 16 GB | $0.17 | 94 |
| RTX 4090 | 24 GB | $0.34 | 71 |
| L40 | 48 GB | $0.69 | 70 |
| A100 PCIe | 80 GB | $1.19 | 67 |
| RTX Pro 6000 | 96 GB | $1.69 | 57 |
| H100 NVL | 94 GB | $2.59 | 36 |

Community vs Secure (verified on the pricing page): Pro 6000 **$1.69 / $1.99**, 5090
**$0.69 / $0.99**, 4090 **$0.34 / $0.69**, 3090 **$0.22 / $0.46**, A5000 **$0.16 / $0.27**.
Community is a peer-to-peer pool; Secure is T3/T4 datacentre. **No numeric SLA is published
for either** — third-party claims that Community pods are preempted without notice are
UNVERIFIED. Community pods *can be migrated*, which is what makes their IP/port churn worse.

**Availability is the binding constraint, not price.** A live console check showed **"Low"
stock on every single available SKU**, with 4080 / RTX 4000 Ada / H100 PCIe / Pro 6000 MaxQ /
A100 SXM / B300 **Unavailable** outright, and per-pod maxima of 1–4 GPUs. Design for "the
card I want is gone."

## ⚠️ The stop-vs-terminate trap

Verified from [docs.runpod.io/pods/manage-pods](https://docs.runpod.io/pods/manage-pods) and
[storage/types](https://docs.runpod.io/pods/storage/types):

| | **STOP** | **TERMINATE** |
|---|---|---|
| Container disk (`/`) | **lost** — "cleared" on stop/restart | erased |
| Volume disk (`/workspace`) | preserved | **deleted** |
| Network volume | preserved | **persists** |
| GPU | **released, not reserved** | released |

And the billing, which is the surprise:

| Storage | Running | **Stopped** |
|---|---|---|
| Container disk | $0.10/GB/mo | $0 (but data is gone) |
| Volume disk | $0.10/GB/mo | **$0.20/GB/mo — the rate doubles** |
| Network volume | $0.07/GB/mo (<1 TB) | $0.07/GB/mo (unchanged) |

Two verbatim quotes that decide the design:

> *"As long as your Pod is running, that GPU is exclusively reserved for you. When you stop
> your Pod, you release that specific GPU."*

> *"You may be allocated zero GPUs if capacity has changed."*

A restart doesn't cleanly fail — it can return a **zero-GPU pod**: SSH-able, still billing
storage, unable to serve. On a scarce SKU with "Low" stock everywhere, assume this happens.

### The pattern to build instead

**Network volume + terminate.** Keep weights on a network volume ($0.07/GB/mo, survives
termination), terminate the pod when the session ends, and redeploy against the same volume
next time — on whatever card is actually free.

- 60 GB weights: **$4.20/mo** on a network volume vs **$12.00/mo** on a *stopped* volume disk.
- No stranded pod, no zero-GPU trap, and you're free to land on a 3090 today and a 4090
  tomorrow.
- Bonus: if your balance hits $0, pods **with** a network volume are preserved; pods without
  are terminated and "their data cannot be recovered."

Caveat: network volumes are datacentre-scoped in practice, which constrains where you can
redeploy — **UNVERIFIED**, confirm before committing.

### Address the endpoint through the proxy

The HTTP proxy URL `https://[POD_ID]-[PORT].proxy.runpod.net` is **stable across stop/start**.
Direct TCP is not: *"Public IP addresses may change for Community Cloud Pods if your Pod is
migrated or restarted"* and *"External port mappings change whenever your Pod resets."*
Register the **proxy URL** as the deployment's `api_base`, never a raw IP:port.

## Serving stack: vLLM with a native quant

**Not GGUF.** vLLM's own docs: *"GGUF support in vLLM is highly experimental and
under-optimized at the moment, it might be incompatible with other features"* — and it has
**moved out-of-tree** to `vllm-gguf-plugin`. You'd inherit the unoptimised path and lose the
tuned Marlin/FP8 kernels.

**And you don't need to build a quant** — they exist. For Qwen3.6-27B (verified on HF):

| Repo | Format | Downloads |
|---|---|---|
| `Qwen/Qwen3.6-27B-FP8` | FP8 (official) | 6.80M |
| `unsloth/Qwen3.6-27B-NVFP4` | NVFP4 | 2.53M |
| `cyankiwi/Qwen3.6-27B-AWQ-INT4` | AWQ INT4 | 2.12M |
| `nvidia/Qwen3.6-27B-NVFP4` | NVFP4 | 1.79M |
| `Lorbus/Qwen3.6-27B-int4-AutoRound` | AutoRound W4A16 | 595K |
| `Intel/Qwen3.6-27B-int4-AutoRound` | AutoRound (official Intel) | 533K |

**Division of labour:** vLLM + native quant on rented GPUs; **llama.cpp/GGUF stays right for
the BC-250 fleet.** Under concurrency vLLM pulls away hard — roughly 485 vs 148 tok/s at ~10
concurrent and 920 vs 155 at ~50 (third-party benchmarks, UNVERIFIED, and llama.cpp's own
`-np` slots narrow the real gap). Since rental bills by the hour, aggregate tok/s per dollar
is the only metric, and that's exactly where llama.cpp plateaus first.

### Notes on the reference vLLM invocation

Using `Lorbus/Qwen3.6-27B-int4-AutoRound` is a **deliberate, correct** choice: a vanilla
AutoRound run packs `mtp.fc` as INT4, vLLM's `Qwen3_5MTP` loader then skips it
(`fc.qweight` vs expected `fc.weight`), and speculative decoding silently gets **0% draft
acceptance**. That repo dequantizes the MTP head back to BF16 — claimed ~80–90% acceptance,
~2× throughput (vendor claim, UNVERIFIED independently). 18 GB vs ~54 GB BF16.

Three things to get right on Blackwell (RTX 5090 and RTX Pro 6000 are both **SM120**):

- **`--compilation-config.cudagraph_mode none` is currently required on SM120/SM121** —
  `cudaErrorStreamCaptureInvalidated` on the MTP module without it.
- **Qwen3.6 MTP looks nightly-only.** The model card reports testing on `0.19.1rc1.dev39`,
  and vLLM's MTP doc lists only Gemma 4 and MiMo-7B. On a stable release
  `--speculative-config` may be inert. Verify draft acceptance is non-zero before believing
  the 2×.
- **`--quantization auto_round` is likely redundant** — AutoRound checkpoints declare their
  format in config and vLLM reads it (the docs' examples pass no flag). AutoRound output is
  compressed-tensors-compatible, so it rides the optimised wNa16 kernels either way.

`--kv-cache-dtype fp8_e4m3` **is fine here** — the model card recommends it and was authored
on a 5090. The known Blackwell FP8-KV blocker is specific to MLA-attention models
(DeepSeek-style), not Qwen dense/MoE.

**Worth one hour of Pro 6000 time (~$1.69) to settle:** AutoRound+working-MTP vs **NVFP4**
(native Blackwell 4-bit kernels; the two NVFP4 repos have ~4× the downloads). If MTP delivers
its claimed 2×, AutoRound wins; if MTP is nightly-blocked, NVFP4 is the safer default.

## Design

A burst node is a fleet node with a lifecycle and a bill. Most of it already exists:

| Existing primitive | Burst equivalent |
|---|---|
| `register` / `deregister` | same, plus provider metadata |
| `litellm-sync` | unchanged |
| `waking→booting→loading→serving` | `creating→provisioning→loading→serving` |
| `/health` gate before "serving" | poll `/v1/models` on the proxy URL |
| Wake-on-LAN | provider `create pod` (attach network volume) |
| `ssh poweroff` | provider **terminate** |

### Driver interface

```python
class BurstDriver(Protocol):
    def search(self, min_vram_gb: int, **filters) -> list[Offer]: ...
    def create(self, offer: Offer, image: str, volume_id: str) -> BurstNode: ...
    def status(self, node: BurstNode) -> str:      # creating|provisioning|running|zero_gpu|gone
    def endpoint(self, node: BurstNode) -> str:    # https://<id>-8000.proxy.runpod.net/v1
    def terminate(self, node: BurstNode) -> None: ...
    def cost_per_hr(self, node: BurstNode) -> float: ...
```

Note `zero_gpu` is a first-class status, not an error — it's RunPod's documented behaviour
and must be detected and handled (terminate + retry on another SKU), not treated as "running".

`RunPodDriver` → `POST https://rest.runpod.io/v1/pods` (create), `/stop`, `/start`,
`runpodctl pod {stop,start}`. `VastDriver` → `vastai create/show/destroy instance`.

### Registry extension

Burst nodes carry fields physical nodes don't: `provider`, `instance_id`, `volume_id`,
`cost_per_hr`, `created_at`, `max_lifetime_s`, `idle_timeout_s`. Marked ephemeral so
`register`/`deregister` and `litellm-sync` treat them normally while the reaper owns their
lifecycle. Physical-node fields (`mac`, `chassis`, `never_sleep`) are simply absent.

### Session flow (the long-running case)

```
/fleet-power burst up --hours 4 --min-vram 24
  → search offers, pick best $/VRAM with stock
  → create pod, attach network volume (weights already there)
  → poll status until running          (phase 1: provider says running)
  → poll /v1/models on the proxy URL   (phase 2: vLLM actually loaded)
  → register into the node registry, litellm-sync
  → serve …
  → on idle_timeout OR max_lifetime OR `burst down`:
      deregister → drain → litellm-sync → TERMINATE (volume survives)
```

## Seven things that will bite

1. **Idle cost is the whole story.** Pods bill wall-clock, not requests. An orphaned Pro 6000
   is **~$41/day**; a 4090 ~$8/day. The reaper is the feature, not a nice-to-have. It must
   survive a fleetd crash (persist `instance_id` + a hard `max_lifetime_s` deadline) and
   **reconcile against the provider's pod list on startup**, not just its own DB.
   RunPod has **no built-in idle timeout** — they ship a community script instead.
2. **Stop ≠ cheap parking.** Doubled volume rate, wiped container disk, released GPU,
   possible zero-GPU resume. Terminate against a network volume instead.
3. **Weight transfer dominates first-boot.** ~15 s from a network volume vs ~2 min pulling
   from HuggingFace. The volume is what makes hour-scale sessions economic.
4. **Two-phase health check.** Provider `running` ≠ vLLM ready. Poll `/v1/models` before
   registering, or the gateway routes into a loading server. Same rule as the local fleet's
   "serving means a real health check."
5. **Deregister before terminate, with a drain window.** Reverse order strands requests.
6. **Endpoint churn.** Use the stable `proxy.runpod.net` URL; direct IP and external ports
   change on restart/migration, especially on Community.
7. **Availability roulette.** "Low" stock everywhere means `create` can fail or land you on a
   different card than planned. Rank offers by $/VRAM among what's actually in stock, and
   make the model/context config adapt to the VRAM you got.

Plus: **egress pricing is UNVERIFIED** for RunPod and Vast (Lambda explicitly charges none).

## Guardrails to ship with it

The failure mode is *silently spending money*, so these aren't optional:

- **Hard `max_lifetime_s`** per burst node — a deadline, not just an idle timer.
- **Spend ceiling per session and per day**, checked before `create`, with projected cost in
  the confirm prompt (`costPerHr` is in RunPod's create response; Vast exposes `dph_total`).
  RunPod's own default spend limit is $80/hr — far above anything we should allow.
- **Startup reconciliation** — list provider pods on boot; terminate or adopt anything tagged
  ours that the registry doesn't know about.
- **`/fleet-power burst status`** — live pods, elapsed, accrued cost, via the same SSE
  treatment the wake path already gets.
- **Dry-run first** (`burst plan`), matching the existing `/power/plan` pattern.
- **Balance alarm** — at $0 RunPod stops pods; those without a network volume are terminated
  and unrecoverable.

## Alternative providers

| | Vast.ai | Modal | Lambda | Together dedicated |
|---|---|---|---|---|
| 4090 $/hr | **0.14–0.54** | — | — | — |
| Idle billed? | yes | **no** | yes | n/a |
| Billing | per-second | per-second | per-minute | per-hour |
| Egress | UNVERIFIED | UNVERIFIED | **none** | n/a |
| Fit | CLI maps 1:1 to fleetd primitives; cheapest; anonymous-host roulette, no portable volume | true scale-to-zero, but you write a Modal app, not a pod | clean API, no egress fees, H100 $3.99–4.29 | managed OpenAI endpoint, H100 $5.49/hr, zero ops, expensive |

**Fly.io GPU is discontinued** (unavailable after Aug 1) — ruled out.
**RunPod Serverless** gives a permanent OpenAI URL and real scale-to-zero, but: the ~200 ms
FlashBoot figure applies only to *retained* workers (users report ~2 min on an 8B after ~1 min
idle), **max workers auto-drop to 0 after 7 days idle** — silently breaking a rarely-used
gateway backend — and it's 1.5–1.8× the pod rate.

**Recommendation: driver interface; RunPod pods first** (network volume + stable proxy URL are
decisive for hour-scale sessions), **Vast second** for cost-sensitive bulk.

Sources: [RunPod manage-pods](https://docs.runpod.io/pods/manage-pods) ·
[storage types](https://docs.runpod.io/pods/storage/types) ·
[pods pricing](https://docs.runpod.io/pods/pricing) ·
[expose-ports](https://docs.runpod.io/pods/configuration/expose-ports) ·
[runpod.io/pricing](https://www.runpod.io/pricing) ·
[create pod API](https://docs.runpod.io/api-reference/pods/POST/pods) ·
[FlashBoot](https://www.runpod.io/blog/serverless-gpu-cold-starts-flashboot) ·
[vLLM GGUF](https://docs.vllm.ai/en/stable/features/quantization/gguf/) ·
[vLLM quantization](https://docs.vllm.ai/en/latest/features/quantization/) ·
[vLLM MTP](https://docs.vllm.ai/en/latest/features/speculative_decoding/mtp/) ·
[Lorbus/Qwen3.6-27B-int4-AutoRound](https://huggingface.co/Lorbus/Qwen3.6-27B-int4-AutoRound) ·
[Vast.ai CLI](https://docs.vast.ai/cli) · [Modal pricing](https://modal.com/pricing) ·
[Lambda](https://lambda.ai/service/gpu-cloud)
