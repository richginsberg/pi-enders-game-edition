# Control-plane standup (bare Ubuntu → running gateway)

Reproducible standup of a divide-and-conquer control-plane box. This is the box that
runs the **LiteLLM gateway**, **fleetd**, the **context sidecar**, and (optionally) a
**self-hosted embedding server**. It does *not* run inference for the squads — that's
the GPU hosts, provisioned separately via fleetd's plays.

Everything here was distilled from standing up the first box (an i7-9700T micro form
factor, Ubuntu 26.04, no GPU). The gotchas below are real, not hypothetical.

## What you get

| Service | Port | Unit | Purpose |
|---|---|---|---|
| LiteLLM gateway | 4000 | `dnc-litellm.service` | OpenAI-compatible tiered routing (`tier:s0..s3`) |
| fleetd | 7431 | `dnc-fleetd.service` | inventory, discovery, IaC plays, task ledger |
| context sidecar | 7432 | `dnc-context.service` | pgvector long-term memory (`/recall`, `/distill`) |
| embedding server | 8090 | `dnc-embeddings.service` | Qwen3-Embedding via llama.cpp CPU (optional) |

## Prerequisites
- Ubuntu box, SSH access, a user with sudo (sudo only for systemd + Postgres).
- Outbound internet (uv, PyPI, HuggingFace, GitHub, provider APIs).

## Getting the repo onto the box
A bare box has **no git**. Pick one:
- `git clone` if git is installed;
- from a machine that has the repo: `scp -r divide-and-conquer user@box:~/` (no rsync on bare boxes);
- or `curl -L <repo-tarball-url> | tar xz`.

## Steps

```bash
# 1. No-sudo bootstrap: uv, Python 3.11 venv, install all four services, config/.env templates.
cd ~/divide-and-conquer
./deploy/bootstrap.sh

# 2. Fill in secrets (the script created this, chmod 600):
$EDITOR ~/dnc/.env      # LITELLM_MASTER_KEY, GLM_API_KEY, DNC_S1_API_BASE

# 3. Install + start the core services (sudo). The committed units are TEMPLATES with
#    __DNC_HOME__/__DNC_USER__ placeholders (no local paths in this public repo);
#    bootstrap.sh already rendered real ones into ~/dnc/systemd/.
sudo cp ~/dnc/systemd/dnc-litellm.service ~/dnc/systemd/dnc-fleetd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dnc-litellm dnc-fleetd

# 4. Verify:
curl -s localhost:4000/health/readiness          # LiteLLM
curl -s localhost:7431/healthz                    # fleetd
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Point Pi extensions at it: `DNC_LITELLM_URL=http://<box>:4000`, `DNC_FLEETD_URL=http://<box>:7431`.

## Verify the standup is complete (all three packages)
`bootstrap.sh` installs `router`, `fleetd`, and `context` **editable** in one step
(`uv pip install -e router fleetd context`). A box set up by hand — or where the repo
wasn't fully present at bootstrap time — can end up with only the gateway installed,
which looks fine (LiteLLM answers on :4000) but leaves the context sidecar and fleetd
uninstalled. Symptom: **Pi slash commands that hit the context/fleetd services (e.g.
`/recall`, `/remember`, `/distill`) silently return nothing.** Confirm all three:

```bash
~/dnc/.venv/bin/python -c "import dnc_router.strategy, fleetd.api, context.api; print('all three OK')"
```

If that raises `ModuleNotFoundError`, the box is a partial standup. Repair it by placing
the missing package source under `~/dnc/<pkg>` and installing editable **with uv** (the
uv-managed venv has no `pip`):

```bash
# copy the missing subtree(s) to the box first (tar-pipe; bare boxes have no rsync):
#   tar czf - -C <repo> fleetd | ssh <box> 'mkdir -p ~/dnc/fleetd && tar xzf - -C ~/dnc/fleetd'
export PATH="$HOME/.local/bin:$PATH"
VIRTUAL_ENV="$HOME/dnc/.venv" uv pip install -e ~/dnc/fleetd -e ~/dnc/context
[ -f ~/dnc/hosts.yaml ] || cp ~/dnc/fleetd/hosts.example.yaml ~/dnc/hosts.yaml   # fleetd needs it
```

## Validate auto-start survives a reboot
The units are `enable`d (symlinked into `multi-user.target.wants`) and — being **system**
units with `WantedBy=multi-user.target` — start at boot with no login/linger required.
Prove it end-to-end rather than trusting `is-enabled`:

```bash
BOOT=$(cat /proc/sys/kernel/random/boot_id); sudo systemctl reboot
# after it comes back (fresh boot_id):
for u in dnc-litellm dnc-fleetd dnc-context; do echo "$u: $(systemctl is-active $u)"; done
curl -s localhost:4000/health/readiness; curl -s localhost:7431/healthz; curl -s localhost:7432/healthz
```
All three should be `active` within seconds of boot, and `/healthz` on the context
sidecar returns `{"ok":true}` only when Postgres/pgvector is reachable.

## Optional: context store (pgvector)

```bash
# Postgres + pgvector (sudo):
sudo apt-get update && sudo apt-get install -y postgresql postgresql-16-pgvector
sudo -u postgres createuser -s "$USER" 2>/dev/null || true
createdb dnc_context
psql dnc_context -c 'CREATE EXTENSION IF NOT EXISTS vector;'
# set DNC_PG_DSN in ~/dnc/.env (default postgresql:///dnc_context works for local peer auth), then:
sudo cp ~/dnc/systemd/dnc-context.service /etc/systemd/system/   # rendered by bootstrap.sh
sudo systemctl daemon-reload && sudo systemctl enable --now dnc-context
```

## Optional: self-hosted embeddings

```bash
./deploy/install-embeddings.sh        # no sudo; downloads llama.cpp + libgomp + model
~/dnc/embeddings/start-embeddings.sh & # smoke test, then curl localhost:8090/v1/embeddings
# durable: sudo cp ~/dnc/systemd/dnc-embeddings.service /etc/systemd/system/ (rendered), then enable it.
```
Benchmarked primary (see `tools/bench_embed.py`): fast enough on CPU to not need a GPU.

## Gotchas baked into these scripts (why they look the way they do)

- **System Python may be 3.14 with no pip** and too new for LiteLLM wheels → we use
  `uv`'s managed **Python 3.11**, installed as a static binary (no sudo).
- **fleetd + SQLite across threads**: fleetd opens its connection with
  `check_same_thread=False` — FastAPI serves sync endpoints from a threadpool, and
  without this every write 500s. (Fixed in `fleetd/db.py`.)
- **llama.cpp needs `libgomp.so.1`** which a bare box lacks; `install-embeddings.sh`
  fetches it **without sudo** via `apt-get download libgomp1` + `dpkg-deb -x`.
- **llama.cpp release assets are `.tar.gz`** (`llama-b<N>-bin-ubuntu-x64.tar.gz`), not `.zip`.
- **llama-server can exit on stdin EOF** when detached from a shell; under systemd
  (no controlling terminal) it's fine — but the unit documents the `tail -f /dev/null`
  wrapper if you ever see it exit on start.
- **`tier:auto` + the affinity router is NOT a proxy-YAML feature** — LiteLLM registers
  a custom strategy only via `Router.set_custom_routing_strategy()`. The launcher
  `python -m dnc_router.serve` wraps the proxy and attaches it; the `dnc-litellm.service`
  unit runs the launcher, so `tier:auto` works out of the box. Plain `litellm --config`
  still serves the explicit tiers (`tier:s0..s3`) if you ever bypass the launcher.
- **Composer 2.5 via Grok does not fit LiteLLM**: the grok-pi extension talks to
  `cli-chat-proxy.grok.com/v1` with the `openai-responses` API and `grok login` token
  auth. Use it directly in Pi, not through the gateway.

## Durability note
These units make the services survive reboots and crashes (`Restart=on-failure`).
Secrets stay in `~/dnc/.env` (chmod 600, gitignored) via `EnvironmentFile=` — never in
the committed unit files.
