"""IaC plays: idempotent SSH/Docker actions against a host (Marionette-style push).

Each play is a sequence of steps; every step checks current state before acting,
so re-running a play converges instead of duplicating work. M3 scope.
"""

from __future__ import annotations

import asyncssh

from .models import Deployment, GpuArch, Host, ServerKind

# Image per (server, gpu_arch). BC-250 gets a custom-built image (ROCm gfx1013
# override or Vulkan backend) — see PLAN.md §6.
IMAGES: dict[tuple[ServerKind, GpuArch], str] = {
    (ServerKind.VLLM, GpuArch.VOLTA): "vllm/vllm-openai",
    (ServerKind.VLLM, GpuArch.AMPERE): "vllm/vllm-openai",
    (ServerKind.LLAMACPP, GpuArch.PASCAL): "ghcr.io/ggml-org/llama.cpp:server-cuda",
    (ServerKind.LLAMACPP, GpuArch.AMPERE): "ghcr.io/ggml-org/llama.cpp:server-cuda",
    (ServerKind.LLAMACPP, GpuArch.RDNA2_BC250): "dnc/llamacpp-bc250",  # custom build
}


async def run(host: Host, command: str) -> asyncssh.SSHCompletedProcess:
    async with asyncssh.connect(host.address, port=host.ssh_port, username=host.ssh_user) as conn:
        return await conn.run(command, check=False)


async def preflight(host: Host) -> dict:
    """Verify SSH reachability, driver, and Docker on a (new) host."""
    checks = {
        "docker": "docker info --format '{{.ServerVersion}}'",
        "gpu": "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || rocm-smi --showproductname",
    }
    results = {}
    for name, cmd in checks.items():
        proc = await run(host, cmd)
        results[name] = {"ok": proc.exit_status == 0, "output": (proc.stdout or "").strip()}
    return results


async def deploy(host: Host, dep: Deployment) -> None:
    """Deploy or upgrade an inference server container. Idempotent by container name."""
    # TODO(M3):
    # 1. resolve image from IMAGES + dep.server_version tag
    # 2. pull image, download model (hf transfer / gguf shards) with checksum
    # 3. render run command (tensor-parallel, ctx, quant, port)
    # 4. docker rm -f + docker run with restart=unless-stopped
    # 5. poll /health until healthy, then register with LiteLLM
    raise NotImplementedError("M3")
