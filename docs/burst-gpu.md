# Burst GPU — renting compute on demand

**Status: designed, not built.** This is the plan for spinning up a rented GPU when there's
sustained work for it, registering it into the gateway as a tier, and destroying it when
idle — reusing the same registry → `litellm-sync` plumbing the physical nodes already use.

> Prices/API details verified **2026-07-25** from live pages. Marked UNVERIFIED where we
> could not read a number off a page. Re-check before committing spend.

## Read this before building it

The break-even math says **burst GPU is not worth it for the cheap tiers**, which is the
opposite of the intuition that sent us looking. Worth internalising before writing code.

Break-even is simply: `sustained output tok/s = hourly_rate ÷ price_per_output_token`.
Below that rate, the hosted API is cheaper — because you pay rental for wall-clock time,
not for tokens.

| You'd be displacing | API $/M out | Vast 4090 $0.25/hr | RunPod 4090 $0.69/hr | RunPod A100 $1.39/hr |
|---|---|---|---|---|
| **S3** `gpt-oss-120b` | 0.17 | **409 tok/s** | 1,127 tok/s | 2,271 tok/s |
| **S2** `deepseek-v4-flash` | 0.188 | 369 tok/s | 1,020 tok/s | 2,054 tok/s |
| **S1** `claude-sonnet-5` | 10.00 | 6.9 tok/s | 19 tok/s | **39 tok/s** |
| **S0** `claude-opus-5` | 25.00 | 2.8 tok/s | 7.7 tok/s | 15 tok/s |

Two conclusions:

1. **You cannot beat S2/S3 APIs by renting.** Undercutting `gpt-oss-120b` at $0.17/M means
   holding **~400–2,300 tok/s sustained**, every hour you pay for. That needs heavy
   continuous batching *and* no idle gaps. Open-weight bulk inference is already priced
   below what rented silicon costs to run — the providers are subsidising it or running at
   far better utilisation than you will.
2. **You can trivially beat S0/S1 — if quality holds.** Displacing Sonnet 5 needs only
   ~39 tok/s on an A100. The catch is the honest one: a rented 24 GB card runs a ~30 B
   4-bit model, which is S2/S3-quality, not S1. The economics only work if an open model
   you can *fit* actually does the job you were paying S1 for.

So the real cases for burst GPU are narrow and specific:

- **Saturated fan-out.** [Team fan-out](team-fanout-prompt.md) spawns 8+ parallel workers —
  a genuinely batched workload that can hold high tok/s for a bounded window. Spin up, run
  the burst, destroy.
- **Work you can't send to an API** — data residency, client confidentiality, licence terms.
- **Models no API hosts** — your own fine-tune, a custom REAP quant, a research checkpoint.
- **Sustained high-volume S1-quality work** where a 70B+ on an A100/H100 is good enough.

If none of those apply, **stay on the hosted tiers** — that's the honest answer, and it's
why this is a design doc rather than shipped code.

## Provider comparison

| | **Vast.ai** | **RunPod pods** | **RunPod Serverless** | **Modal** | **Lambda** |
|---|---|---|---|---|---|
| 4090 $/hr | **0.14–0.54** | 0.69 | 1.10 | — | — |
| A100 80 GB $/hr | 0.60–1.07¹ | 1.39 | 2.72 | ~2.50 | 1.99–2.79 |
| H100 $/hr | 1.74–3.47¹ | 2.89–2.99 | 4.55 | ~3.95 | 3.99–4.29 |
| Billing | per-second | per-second | per-second | per-second | per-minute |
| **Idle billed?** | **yes** | **yes** | only during idle-timeout | **no** | **yes** |
| Scale to zero | manual destroy | manual destroy | **automatic** | **automatic** | manual |
| Managed OpenAI URL | ✗ (raw host:port) | ✗ | ✓ stable URL | ✗ (Modal app) | ✗ |
| Persistent weights | ✗ per-host only | ✓ network volume | ✓ cached models | ✓ | ✓ |
| Egress | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | **none** |

¹ Vast figures are live marketplace bundles pulled from the public API; A100/H100 entries
may be multi-GPU nodes — per-GPU normalisation not verified. Single 4090s were clearly
$0.14–0.54, i.e. **well under RunPod**.

**Fly.io GPU is discontinued** (unavailable after Aug 1) — ruled out.
**Together dedicated endpoints** (H100 $5.49/hr) are managed-and-expensive; only worth it
if you want zero ops.

### Which to build against

- **Vast.ai — best structural fit.** Its CLI maps almost 1:1 onto primitives fleetd already
  has: `create instance` ≈ Wake-on-LAN, `show instance` ≈ health poll, `ssh-url` ≈ our
  existing SSH transport, `destroy` ≈ sleep. We already run vLLM/llama.cpp ourselves, so
  the missing managed endpoint costs us nothing. Cheapest by a wide margin.
  Cost: anonymous-host roulette, no cross-host weight cache.
- **RunPod pods — best reliability.** One clean REST call returns `id`, `publicIp`,
  `portMappings`, `costPerHr`. **Network storage** ($0.07/GB/mo) holds pre-downloaded
  weights and cuts startup from ~2 min (HF download) to **~15 s** for a 7B. Predictable
  hardware, ~1.3–2× Vast's price.
- **RunPod Serverless — tempting, but a trap for this use case.** It gives a permanent
  `https://api.runpod.ai/v2/<id>/openai/v1` you could register in LiteLLM once and forget,
  with automatic scale-to-zero. But: the advertised **~200 ms FlashBoot only applies to
  retained workers** (real users report ~2 min on an 8B after ~1 min idle, and eviction
  under GPU contention), and **after 7 days without requests RunPod sets max workers to 0**,
  silently breaking a rarely-used gateway backend. Also 1.5–1.8× the pod rate, so any duty
  cycle above ~40% is cheaper as a pod.
- **Modal — the only true no-idle-charge option.** Per-second with zero idle cost, real
  autoscaling. The cost is architectural: you write a Modal app in Python decorators rather
  than launching a generic pod, so it doesn't reuse the fleetd SSH/pod model at all.

**Recommendation: implement a driver interface, ship Vast first, RunPod pods second.**

## Design

A burst node is just a fleet node with a lifecycle and a bill. The existing pieces carry
most of it:

| Existing primitive | Burst equivalent |
|---|---|
| `register` / `deregister` (node registry) | same, plus provider metadata |
| `litellm-sync` (regenerate gateway entries) | unchanged |
| Power state machine `waking→booting→loading→serving` | `creating→provisioning→loading→serving` |
| `/health` gate before "serving" | poll `/v1/models` on the rented endpoint |
| Wake-on-LAN | provider `create instance` |
| `ssh poweroff` | provider `destroy instance` |

### Provider driver interface

```python
class BurstDriver(Protocol):
    def search(self, gpu: str, **filters) -> list[Offer]: ...
    def create(self, offer: Offer, image: str, model: str) -> BurstNode: ...
    def status(self, node: BurstNode) -> str:      # creating|provisioning|running|gone
    def endpoint(self, node: BurstNode) -> str:    # http://host:port/v1
    def destroy(self, node: BurstNode) -> None: ...
    def cost_per_hr(self, node: BurstNode) -> float: ...
```

`VastDriver` shells the CLI; `RunPodDriver` calls `POST https://rest.runpod.io/v1/pods`.

### Registry extension

Burst nodes need fields physical nodes don't: `provider`, `instance_id`, `cost_per_hr`,
`created_at`, `max_lifetime_s`, `idle_timeout_s`. They're marked ephemeral so
`register`/`deregister` and `litellm-sync` treat them normally while the reaper owns their
lifecycle. Physical-node fields (`mac`, `chassis`, `never_sleep`) are simply absent.

### Vast reference flow

```bash
vastai search offers 'gpu_name=RTX_4090 num_gpus=1 verified=true direct_port_count>=1' \
      -o 'dlperf_usd-'                       # best perf-per-dollar first
vastai create instance <OFFER_ID> --image vllm/vllm-openai:latest --disk 60 --ssh --direct
vastai show instance <ID>                    # loading -> running  (phase 1)
curl -sf http://<ip>:<port>/v1/models        # vLLM actually ready (phase 2)
# → register into the registry, litellm-sync, serve traffic …
vastai destroy instance <ID>                 # stop billing entirely
```

### RunPod reference flow

```bash
curl -X POST https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -d '{"name":"burst-1","imageName":"vllm/vllm-openai:latest",
       "gpuTypeIds":["NVIDIA GeForce RTX 4090"],"gpuCount":1,
       "containerDiskInGb":50,"volumeInGb":20,"ports":["8000/http","22/tcp"]}'
# 201 → { id, publicIp, portMappings, costPerHr }
```

## Six things that will bite

Learned from the research, and from the failure modes the local fleet already taught us:

1. **Idle cost is the entire cost story.** RunPod pods and Vast instances bill wall-clock,
   not requests. **An orphaned 4090 pod is ~$17/day.** The idle reaper is not a nice-to-have
   — it is the feature. It must survive a fleetd crash (persist `instance_id` + a hard
   `max_lifetime_s` deadline so a restarted daemon can still find and kill strays), and it
   should reconcile against the provider's instance list on startup, not just its own DB.
2. **Weight transfer dominates startup.** ~15 s from a RunPod network volume vs ~2 min
   downloading from HuggingFace. Budget a persistent volume or a baked image. On Vast
   neither is portable across hosts — you re-download per instance.
3. **Two-phase health check.** Provider `running` ≠ vLLM ready. Poll `/v1/models` on the
   endpoint before registering into LiteLLM, or the gateway routes into a loading server.
   This is exactly the `serving`-means-a-real-health-check rule the local fleet already
   enforces — and the same reason the deploy play gates on a real completion.
4. **Deregister before destroy, with a drain window.** Pull it from the gateway, let
   in-flight requests finish, *then* destroy. Reverse order strands requests.
5. **Handle unsolicited disappearance.** Vast interruptible instances can be **preempted
   mid-request** — a failure mode LAN nodes never had. Don't use interruptible as a LiteLLM
   backend without retry/failover, and treat "instance vanished" as a normal event.
6. **Stopped ≠ free.** On both RunPod and Vast, a stopped instance still bills storage.
   **Destroy, don't stop**, unless you're re-waking within minutes.

Plus: **egress pricing is UNVERIFIED** on RunPod and Vast (Lambda explicitly charges none).
Check before streaming heavy token volume.

## Guardrails to ship with it

Given that the failure mode is *silently spending money*, the safety features are not
optional extras:

- **Hard `max_lifetime_s`** on every burst node — a deadline, not just an idle timer.
- **Spend ceiling per burst and per day**, enforced before `create`, with the projected
  cost shown in the confirm prompt (`costPerHr` comes back in RunPod's create response;
  Vast exposes `dph_total`).
- **`/fleet-power burst status`** showing live instances, elapsed, and accrued cost — the
  same live-SSE treatment the wake path already gets.
- **Startup reconciliation** — on boot, list provider instances and destroy/adopt anything
  tagged as ours that the registry doesn't know about.
- **Dry-run first** (`burst plan`), matching the existing `/power/plan` pattern.

Sources: [RunPod pricing](https://www.runpod.io/pricing) ·
[RunPod create pod API](https://docs.runpod.io/api-reference/pods/POST/pods) ·
[RunPod endpoint config](https://docs.runpod.io/serverless/endpoints/endpoint-configurations) ·
[FlashBoot cold starts](https://www.runpod.io/blog/serverless-gpu-cold-starts-flashboot) ·
[worker-vllm #111](https://github.com/runpod-workers/worker-vllm/issues/111) ·
[Vast.ai pricing](https://vast.ai/pricing) · [Vast.ai CLI](https://docs.vast.ai/cli) ·
[Lambda GPU cloud](https://lambda.ai/service/gpu-cloud) · [Modal pricing](https://modal.com/pricing) ·
[Fly.io GPU (discontinued)](https://fly.io/docs/gpus/gpu-quickstart/) ·
[Together pricing](https://www.together.ai/pricing)
