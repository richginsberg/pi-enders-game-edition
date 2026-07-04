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
```bash
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

```bash
MODEL=$(readlink -f ~/models/Qwen3-Coder-30B-A3B-Instruct-Pruned-Q4_K_M.gguf)
podman run -d --name dnc-bc250 \
  --security-opt label=disable \
  --device /dev/dri/renderD128 \
  -p 8080:8080 \
  -v "$MODEL":/models/model.gguf:ro \
  -v ~/templates:/templates:ro \
  -e DNC_MODEL=/models/model.gguf \
  -e DNC_CHAT_TEMPLATE=/templates/Qwen-Qwen3-Coder-30B-A3B-Instruct-tool_use.jinja \
  -e DNC_CTX=65536 \
  dnc/llamacpp-bc250:latest

# Confirm the GPU was found, THEN recreate with --restart unless-stopped:
podman logs dnc-bc250 2>&1 | grep -iE 'radeon|radv|vulkan.*device'
# If it thrashed the node: recover, then `podman rm -f dnc-bc250`.
```

## GPU access: rootless is NOT enough on this node
Confirmed on the BC-250: **rootless podman gets only `llvmpipe` (CPU)** — RADV loads but
`failed to initialize winsys`. `renderD128` is world-rw but `card1` is `root:video` and
the run user isn't in `video`/`render`, so amdgpu winsys can't init under the user
namespace. With no GPU, `-ngl 99` thrashes the 3.5 GiB host. Use ONE of:
- **Rootful (simplest):** `sudo podman run ... --device /dev/dri ...` (root accesses card1).
- **Rootless with groups:** `sudo usermod -aG video,render $USER` + re-login, then pass
  `--device /dev/dri/renderD128 --device /dev/dri/card1 --group-add keep-groups`.

Always **probe first** (no model, no thrash):
```bash
podman run --rm --device /dev/dri --entrypoint vulkaninfo dnc/llamacpp-bc250:latest --summary \
  | grep -iE 'deviceName|driverName'   # must show Radeon / radv, NOT llvmpipe
```

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
