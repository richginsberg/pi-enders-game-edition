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

## The GPU gotcha: stock Mesa doesn't know this chip (THE key finding)
Verified on the node: with stock upstream Mesa (what `dnf install mesa-vulkan-drivers`
gives), RADV loads but reports `amdgpu: unknown (family_id, chip_external_rev): (143,132)`
and `failed to initialize winsys` → Vulkan shows only **`llvmpipe` (CPU)**, so `-ngl 99`
falls back to host RAM and **thrashes the 3.5 GiB box**. The host works because it has a
**BC-250-patched Mesa** (a locally-installed `mesa-vulkan-drivers` with the same version
string but a patched `libvulkan_radeon.so` that maps this chip to NAVI10).

Fix (until the patched Mesa is baked into the image — see below): **bind-mount the host's
patched RADV driver** over the container's stock one. Also pass both DRM nodes with the
host groups (needs `sudo usermod -aG video,render $USER` + re-login once).

Always **probe first** (no model → no thrash if the GPU is missing):
```bash
podman run --rm --security-opt label=disable \
  --device /dev/dri --group-add keep-groups \
  -v /usr/lib64/libvulkan_radeon.so:/usr/lib64/libvulkan_radeon.so:ro \
  --entrypoint vulkaninfo dnc/llamacpp-bc250:latest --summary | grep -iE 'deviceName'
# MUST show "AMD Radeon Graphics (RADV NAVI10)", NOT llvmpipe. Verified: ~79 tok/s.
```

**Proper fix (follow-up):** obtain the BC-250-patched Mesa RPM(s) and install them in the
Dockerfile so the image is self-contained (no host bind-mount). The host has them as a
local `mesa-vulkan-drivers-24.1.5-2.fc40` (no upstream repo).

## Start / stop / status (the validated operational commands)

Pass the **whole `/dev/dri`** (DRM card node numbering — `card0`/`card1` — is not stable
across reboots) and the host's patched RADV driver. This is the exact recipe verified on
the node (~79 tok/s, 24 CU).

**Create + start** (first time, or after `rm`):
```bash
MODEL=$(readlink -f ~/models/Qwen3-Coder-30B-A3B-Instruct-Pruned-Q4_K_M.gguf)
podman run -d --name dnc-bc250 --restart unless-stopped \
  --security-opt label=disable \
  --device /dev/dri --group-add keep-groups \
  -v /usr/lib64/libvulkan_radeon.so:/usr/lib64/libvulkan_radeon.so:ro \
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
