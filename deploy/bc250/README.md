# BC-250 llama.cpp container (Vulkan)

Containerized llama.cpp for the AMD BC-250 S3 node, using the **Vulkan/RADV** backend
(ROCm not needed). Verified bare-metal on the node: Qwen3-Coder-30B-A3B q4 at ~74 tok/s.

## Key constraints (why this is shaped the way it is)
- **BC-250 = 16 GB unified memory, BIOS-partitioned**: ~12 GB to GPU VRAM, only **3.5 GB
  to the host**. The container runtime + process live in that 3.5 GB, and it doubles as
  GTT spill for the GPU. Keep host RAM free; don't bake weights into the image.
- **Fits at the edge**: q4 weights (~8.9 GiB) + q8_0 KV @ 65536 ctx (~3.2 GiB) ≈ 12.1 GiB
  vs 12 GiB VRAM. Compute buffers spill to GTT. Lower `DNC_CTX` if you hit OOM.
- **`--flash-attn` is required** for the q8_0 V-cache (baked into the entrypoint).
- **RADV is userspace** → build the image on any x86_64 host; the GPU is only needed at
  run time (via `/dev/dri/renderD128`, group `render`). ROCm's `/dev/kfd` is NOT needed.

## Build (on a box with docker/podman — e.g. the dev box)
The image bakes in the BC-250-patched RADV driver (`libvulkan_radeon.so`), which is a
vendored binary (gitignored). Fetch it into the build context first:
```bash
./deploy/bc250/fetch-patched-driver.sh <user>@<bc250-host>   # copies the patched .so here
docker build -t dnc/llamacpp-bc250:latest deploy/bc250
# pin llama.cpp:  --build-arg LLAMA_CPP_REF=b9870
```

## Get the image onto the BC-250 (Fedora 40, install a runtime first)
```bash
# on the BC-250 (needs sudo once):
sudo dnf -y install podman        # Fedora-native, rootless, no daemon (kind to 3.5GB RAM)

# transfer the image (no registry needed):
docker save dnc/llamacpp-bc250:latest | ssh <user>@<bc250-host> 'podman load'
```

## Run on the BC-250
> **DANGER — 3.5 GiB host RAM.** If the GPU is NOT detected inside the container,
> llama.cpp falls back toward host memory and thrashes the box until it's unreachable
> over SSH. **First run without `--restart`** (so a bad run can't relaunch-loop), watch
> the log for a `Radeon ... (RADV ...)` device line, and only add `--restart` once it's
> confirmed serving. Model weights are a HF-cache symlink — mount the RESOLVED blob, not
> the symlink (a bind-mount won't follow it out of `~/models`). SELinux is Enforcing, so
> `--security-opt label=disable` (renderD128 is world-rw) or relabel mounts with `:ro,Z`.

## Host-RAM / `--parallel` budget (host RAM, not VRAM, is the ceiling)
The VRAM fit (weights + KV ≈ 12 GB) is only half the story. llama.cpp's **per-slot** KV, compute,
and GTT-spill buffers live in the **3.5 GB host** partition and scale with `--parallel × ctx` — so
concurrency, not VRAM, is what OOMs this node.

Observed: a model that loads and smoke-tests fine will die **`Exited (137)`** under concurrent
long-context load (`dmesg`: `Out of memory: Killed process (llama-server)`, `global_oom`) while the
GPU is perfectly healthy. The box stays pingable/SSH-able — only port 8080 dies (`connection
refused`) — so it reads like a crash but it's a host-RAM OOM.

Rules:
- **`--parallel 1`** and **`-c ≤ 16384`** by default. Four slots ≈ 4× the host buffers → OOM.
- **Never `--no-mmap`** (mallocs ~9.3 GB weights into host RAM → instant OOM). Keep mmap.
- **`free -h` under load**; back off if `available` → 0.
- **Agentic harnesses (Terminal-Bench) → `--n-concurrent 1`.** The 3.5 GB host caps effective
  concurrency at ~1 regardless of model.
- **Curl on IPv4** (`127.0.0.1`): `-p 8080:8080` publishes IPv4 only; rootless podman resets the
  `::1` (IPv6) path — an easy false "crash".

## Reasoning models: `--jinja` is REQUIRED, plus generation caps
A **reasoning model** (e.g. Qwen3.6) served **without `--jinja`** never terminates: llama.cpp
renders a default template that doesn't apply the model's `<|im_end|>` end-of-turn token, so the
model finishes thinking but never stops — it runs to the context limit (measured: 50k+ tokens on a
trivial prompt, wedging the single slot for ~20 min). `--jinja` makes it use the embedded chat
template + reasoning parsing, and it stops cleanly (`finish_reason: stop`). Always serve with:
```
--jinja \
--parallel 1 \                         # single-slot = accurate fleet scenario (24 nodes)
-n 8192 \                              # total generation cap: outer runaway guard
--repeat-penalty 1.1 --repeat-last-n 256 \   # break repetition loops (default penalty is OFF)
--reasoning-budget 4096                # cap THINKING (not total) so a hard prompt still emits an
                                       # answer instead of hitting -n mid-thought with empty content
```
`-n` caps total tokens; `--reasoning-budget` caps only the think channel then forces the answer —
you need both. These are baked into the fleetd BC-250 play (`server_args`); set them by hand only
for manual `podman run`s. Harmless no-ops for non-reasoning models (the coder has no think channel).

## The GPU gotcha: stock Mesa doesn't know this chip (THE key finding)
Verified on the node: with stock upstream Mesa (what `dnf install mesa-vulkan-drivers`
gives), RADV loads but reports `amdgpu: unknown (family_id, chip_external_rev): (143,132)`
and `failed to initialize winsys` → Vulkan shows only **`llvmpipe` (CPU)**, so `-ngl 99`
falls back to host RAM and **thrashes the 3.5 GiB box**. The host works because it has a
**BC-250-patched Mesa** (a locally-installed `mesa-vulkan-drivers` with the same version
string but a patched `libvulkan_radeon.so` that maps this chip to NAVI10).

**Fix (now baked into the image):** the Dockerfile `COPY`s the patched
`libvulkan_radeon.so` over stock Mesa's, so the image is **self-contained — no runtime
driver bind-mount**. You just pass the DRM device + host groups (needs
`sudo usermod -aG video,render $USER` + re-login once so the container can open the render node).

Always **probe first** (no model → no thrash if the GPU is missing):
```bash
podman run --rm --security-opt label=disable \
  --device /dev/dri --group-add keep-groups \
  --entrypoint vulkaninfo dnc/llamacpp-bc250:latest --summary | grep -iE 'deviceName'
# MUST show "AMD Radeon Graphics (RADV NAVI10)", NOT llvmpipe. Verified end-to-end.
```

## Start / stop / status (the validated operational commands)

Pass the **whole `/dev/dri`** (DRM card node numbering — `card0`/`card1` — is not stable
across reboots). The patched RADV driver is baked into the image, so no driver mount is
needed. This is the exact recipe verified on the node (~79 tok/s, 24 CU).

**Create + start** (first time, or after `rm`):
```bash
MODEL=$(readlink -f ~/models/Qwen3-Coder-30B-A3B-Instruct-Pruned-Q4_K_M.gguf)
podman run -d --name dnc-bc250 --restart unless-stopped \
  --security-opt label=disable \
  --device /dev/dri --group-add keep-groups \
  -p 8080:8080 \
  -v "$MODEL":/models/model.gguf:ro \
  -v ~/templates:/templates:ro \
  --entrypoint llama-server dnc/llamacpp-bc250:latest \
    -m /models/model.gguf -ngl 99 -c 65536 \
    --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --temp 0.6 \
    --host 0.0.0.0 --port 8080 --alias Qwen-Qwen3-Coder-30B-A3B-Instruct \
    --jinja --chat-template-file /templates/Qwen-Qwen3-Coder-30B-A3B-Instruct-tool_use.jinja
```
> `--entrypoint llama-server` with explicit flags is used because the currently-deployed
> image predates the `--flash-attn on` entrypoint fix; an image rebuilt from current
> `deploy/bc250/` runs the env-driven entrypoint directly (just drop the `--entrypoint …`
> override and the trailing flags).

**Everyday start/stop** (once the container exists):
```bash
podman stop dnc-bc250        # stop  (also frees the GPU)
podman start dnc-bc250       # start
podman restart dnc-bc250     # restart
podman ps --filter name=dnc-bc250 --format '{{.Status}}'   # status
podman logs -f dnc-bc250     # follow logs (Ctrl-C to detach)
podman rm -f dnc-bc250       # remove (needed before re-running `podman run`)
```
It's registered as `tier:s3` in the gateway (`DNC_S3_API_BASE=http://<node>:8080/v1`), so
after `start` it's reachable via `tier:s3` once `curl localhost:8080/health` returns 200.

### Surviving reboots (don't rely on `podman start`)
Two traps: (a) rootless containers do NOT auto-start on boot, and (b) `podman start`
reuses the **create-time** device nodes — but `/dev/dri/cardN` numbering changes across
reboots, so `start` fails with `cannot stat /dev/dri/cardN`. Fix: a **user systemd unit
that recreates the container each boot** (`podman run --rm --replace`, re-reading
`/dev/dri`), plus linger so it runs without a login:
```bash
mkdir -p ~/.config/systemd/user
podman generate systemd --new --name dnc-bc250 --restart-policy=on-failure \
  > ~/.config/systemd/user/dnc-bc250.service
systemctl --user daemon-reload && systemctl --user enable dnc-bc250.service
loginctl enable-linger "$USER"        # start at boot without a login session (no sudo needed)
```
After this, a power-cycle brings `tier:s3` back automatically regardless of card renumbering.

## Verify (once the probe shows a Radeon device)
```bash
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"Qwen-Qwen3-Coder-30B-A3B-Instruct","messages":[{"role":"user","content":"reply OK"}],"max_tokens":8}'
```
Then it's already registered as `tier:s3` in the gateway (`DNC_S3_API_BASE=http://<bc250-host>:8080/v1`).

## Notes / open items
- **Pin `LLAMA_CPP_REF`.** Building master already bit us twice vs the 8-month-old bare-
  metal build: (1) `--flash-attn` now needs a value (`on|off|auto`), and (2) master's
  `common_fit_params` auto memory-fit aborted / fell back toward host RAM and thrashed
  the node. Pin to a tag matching the known-good bare-metal era (or one verified not to
  thrash) before treating this as production.
- fleetd's deploy play targets image `dnc/llamacpp-bc250` for `RDNA2_BC250` and passes
  the Vulkan device flags (see `fleetd/fleetd/plays.py`).
