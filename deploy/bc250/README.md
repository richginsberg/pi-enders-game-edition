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
```bash
podman run -d --name dnc-bc250 --restart unless-stopped \
  --device /dev/dri --group-add keep-groups \
  -p 8080:8080 \
  -v ~/models:/models:ro \
  -v ~/templates:/templates:ro \
  -e DNC_MODEL=/models/Qwen3-Coder-30B-A3B-Instruct-Pruned-Q4_K_M.gguf \
  -e DNC_CHAT_TEMPLATE=/templates/Qwen-Qwen3-Coder-30B-A3B-Instruct-tool_use.jinja \
  -e DNC_CTX=65536 \
  dnc/llamacpp-bc250:latest

# docker equivalent: replace `--group-add keep-groups` with `--group-add render`.
```
`--group-add keep-groups` (podman) preserves the host `render` group so the container
can open `/dev/dri/renderD128`. With docker use `--group-add render` (gid 105 on the node).

## Verify
```bash
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"Qwen-Qwen3-Coder-30B-A3B-Instruct","messages":[{"role":"user","content":"reply OK"}],"max_tokens":8}'
```
Then it's already registered as `tier:s3` in the gateway (`DNC_S3_API_BASE=http://<bc250-host>:8080/v1`).

## Notes / open items
- Building the newest llama.cpp master may drift flag/template behavior vs the 8-month-
  old bare-metal build. Pin `LLAMA_CPP_REF` to a tested tag for stability. Newer weights
  are tracked separately (research task).
- fleetd's deploy play targets image `dnc/llamacpp-bc250` for `RDNA2_BC250` and passes
  the Vulkan device flags (see `fleetd/fleetd/plays.py`).
