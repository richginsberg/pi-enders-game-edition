# dnc-burst

Rent a GPU, saturate it for hours, give it back before it costs you.

A burst tier only makes sense at **full saturation over hours** — a rented GPU bills
wall-clock, so an idle one is pure loss. This package makes that judgement explicitly
(`should_burst`), sizes the deployment from real model geometry (`plan_capacity`), generates
the launch template (`render`), and reaps anything that outlives its usefulness.

```bash
pip install -e .

dnc-burst models                        # servable models + KV geometry
dnc-burst capacity --model qwen3.6-35b-a3b --context 262144 --min-seqs 8
dnc-burst template --model qwen3.6-35b-a3b --gpu l40
dnc-burst switches                      # what every flag does
dnc-burst gate --tokens 50000000 --hours 4 --tok-s 800 --fallback-usd-per-m 10
BURST_ENABLED=true dnc-burst up --model qwen3.6-35b-a3b --replicas 1
dnc-burst status ; dnc-burst reap ; dnc-burst down
```

Mounted into fleetd automatically when installed (`/burst/*`), or run standalone:
`uvicorn burst.api:app`.

## Why it refuses more than it accepts

The gate exists because renting usually loses. It only returns `BURST` when the job is
**millions of output tokens**, over **hours**, at **high saturation**, and **cheaper than the
fallback tier by a margin**:

```
$ dnc-burst gate --tokens 50000000 --hours 4 --tok-s 800 --fallback-usd-per-m 10
BURST: 1 node(s) x 4.0h at $1.69/hr = $6.76, vs $500.00 on the fallback tier (saves $493.24)

$ dnc-burst gate --tokens 50000000 --hours 4 --tok-s 800 --fallback-usd-per-m 0.17
USE_FALLBACK: effective $0.587/M does not beat the fallback $0.170/M (need >2761 tok/s)
```

Renting displaces expensive tiers. It cannot beat the cheap ones — the gate enforces that.

## Safety

Money leaks silently, so the guardrails are the product:

- **Hard `max_lifetime_s`** beats any idle timer (a trickle of traffic resets an idle timer
  forever; a wedged node may never report idle).
- **Idle TTL** scales to zero.
- **Daily spend cap** checked before renting.
- **Startup reconciliation** finds pods the manager forgot — a crash mid-provision leaks a
  node that bills until someone notices.
- **Volume reaper** on its own clock: weights volumes outlive pods by design, so they also
  bill forever if nobody looks.
- **Never registers a node until `/v1/models` answers**, so the gateway never routes into a
  loading server; the tier falls up to the higher tier during cold start.
- **Terminate, never stop** — stopping a RunPod pod doubles the disk rate, clears the
  container disk, releases the GPU and can resume with zero GPUs.

Config: `BURST_ENABLED` (default false), `BURST_MIN_OUTPUT_TOKENS`, `BURST_MIN_HOURS`,
`BURST_IDLE_TTL_S`, `BURST_MAX_LIFETIME_S`, `BURST_MAX_REPLICAS`, `BURST_DAILY_USD_CAP`,
`BURST_VOLUME_MAX_IDLE_DAYS`, `BURST_PER_NODE_TOK_S`, `BURST_PROVIDER`, `RUNPOD_API_KEY`.

Templates and the switch reference: [`docs/burst-templates.md`](../docs/burst-templates.md).
Economics and provider comparison: [`docs/burst-gpu.md`](../docs/burst-gpu.md).
