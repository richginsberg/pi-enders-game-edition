"""IaC plays: idempotent SSH/Docker actions against a host (Marionette-style push).

Every play converges: each step checks current state before acting, so re-running
is safe. Pure command-rendering lives in module functions (unit-testable without
SSH); the async plays do the remote I/O via `run()`.

Lifecycle rules:
- management=docker deployments: full lifecycle (deploy/upgrade/stop).
- management=adopted deployments: NEVER touched by plays; monitor-only.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

import asyncssh

from .models import Deployment, GpuArch, Host, Management, ServerKind

# Image repo per (server, gpu_arch); version tag comes from dep.server_version.
# BC-250 gets a custom-built image (ROCm gfx1013 override or Vulkan backend).
IMAGES: dict[tuple[ServerKind, GpuArch], str] = {
    (ServerKind.VLLM, GpuArch.VOLTA): "vllm/vllm-openai",
    (ServerKind.VLLM, GpuArch.AMPERE): "vllm/vllm-openai",
    (ServerKind.LLAMACPP, GpuArch.PASCAL): "ghcr.io/ggml-org/llama.cpp",
    (ServerKind.LLAMACPP, GpuArch.AMPERE): "ghcr.io/ggml-org/llama.cpp",
    (ServerKind.LLAMACPP, GpuArch.RDNA2_BC250): "dnc/llamacpp-bc250",
}

MODELS_DIR = "/opt/dnc/models"
HEALTH_PATH = {ServerKind.VLLM: "/health", ServerKind.LLAMACPP: "/health"}


def image_ref(host: Host, dep: Deployment) -> str:
    assert host.gpu_arch is not None
    repo = IMAGES[(dep.server, host.gpu_arch)]
    return f"{repo}:{dep.server_version}"


def container_name(dep: Deployment) -> str:
    return f"dnc-{dep.id}"


def server_args(host: Host, dep: Deployment) -> list[str]:
    """Inference-server CLI args from the deployment spec."""
    if dep.server == ServerKind.LLAMACPP:
        args = [
            "-m", f"{MODELS_DIR}/{dep.model_id}",
            "--host", "0.0.0.0",
            "--port", str(dep.port),
            "-c", str(dep.context_window),
            "-ngl", "999",
            "--alias", dep.model_id.split("/")[-1].lower(),
        ]
        if host.gpu_count > 1:
            args += ["--split-mode", "layer"]
    elif dep.server == ServerKind.VLLM:
        args = [
            "--model", dep.model_id,
            "--host", "0.0.0.0",
            "--port", str(dep.port),
            "--max-model-len", str(dep.context_window),
        ]
        if host.gpu_count > 1:
            args += ["--tensor-parallel-size", str(host.gpu_count)]
        if dep.quant:
            args += ["--quantization", dep.quant]
    else:
        raise ValueError(f"no play for server kind {dep.server}")
    return args + dep.extra_args


def docker_run_command(host: Host, dep: Deployment) -> str:
    """Render the idempotent (re)create command for a managed deployment."""
    name = container_name(dep)
    gpu_flag = "--device=/dev/kfd --device=/dev/dri" if host.gpu_arch == GpuArch.RDNA2_BC250 else "--gpus all"
    parts = [
        f"docker rm -f {name} >/dev/null 2>&1 || true;",
        "docker run -d",
        f"--name {name}",
        "--restart unless-stopped",
        gpu_flag,
        f"-v {MODELS_DIR}:{MODELS_DIR}:ro",
        f"-p {dep.port}:{dep.port}",
        "--label dnc.managed=true",
        f"--label dnc.deployment={dep.id}",
        image_ref(host, dep),
        *(shlex.quote(a) for a in server_args(host, dep)),
    ]
    return " ".join(parts)


@dataclass
class PlayReport:
    play: str
    host_id: str
    steps: list[dict] = field(default_factory=list)
    ok: bool = True

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail.strip()[-2000:]})
        self.ok = self.ok and ok


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


async def ensure_model(host: Host, dep: Deployment, report: PlayReport) -> None:
    """Download the model to MODELS_DIR if absent (converges; hf CLI resumes)."""
    target = f"{MODELS_DIR}/{dep.model_id}"
    exists = await run(host, f"test -e {shlex.quote(target)}")
    if exists.exit_status == 0:
        report.step("model", True, "already present")
        return
    # GGUF repo paths ("org/Repo-GGUF/file.gguf") and HF model ids both work via hf download.
    repo = "/".join(dep.model_id.split("/")[:2])
    proc = await run(
        host,
        f"mkdir -p {MODELS_DIR} && "
        f"(hf download {shlex.quote(repo)} --local-dir {shlex.quote(f'{MODELS_DIR}/{repo}')} "
        f"|| huggingface-cli download {shlex.quote(repo)} --local-dir {shlex.quote(f'{MODELS_DIR}/{repo}')})",
    )
    report.step("model", proc.exit_status == 0, proc.stderr or proc.stdout or "")


async def wait_healthy(host: Host, dep: Deployment, report: PlayReport, timeout_s: int = 600) -> None:
    """Poll the server's health endpoint from the host itself until it answers."""
    path = HEALTH_PATH[dep.server]
    proc = await run(
        host,
        f"for i in $(seq 1 {timeout_s // 5}); do "
        f"curl -sf http://localhost:{dep.port}{path} >/dev/null && exit 0; sleep 5; done; exit 1",
    )
    report.step("health", proc.exit_status == 0, "" if proc.exit_status == 0 else "health check timed out")


async def deploy(host: Host, dep: Deployment) -> PlayReport:
    """Deploy or upgrade an inference server container. Idempotent by container name."""
    report = PlayReport(play="deploy", host_id=host.id)
    if dep.management != Management.DOCKER:
        report.step("guard", False, f"refusing to deploy management={dep.management} (adopted servers are monitor-only)")
        return report

    pull = await run(host, f"docker pull {image_ref(host, dep)}")
    report.step("pull", pull.exit_status == 0, pull.stderr or "")
    if not report.ok:
        return report

    await ensure_model(host, dep, report)
    if not report.ok:
        return report

    start = await run(host, docker_run_command(host, dep))
    report.step("start", start.exit_status == 0, start.stderr or "")
    if not report.ok:
        return report

    await wait_healthy(host, dep, report)
    # TODO(M3, task #8/#10): on success, register with LiteLLM (fleetd litellm_sync)
    return report


async def stop(host: Host, dep: Deployment) -> PlayReport:
    report = PlayReport(play="stop", host_id=host.id)
    if dep.management != Management.DOCKER:
        report.step("guard", False, "adopted servers are never stopped by plays")
        return report
    proc = await run(host, f"docker rm -f {container_name(dep)}")
    report.step("stop", proc.exit_status == 0, proc.stderr or "")
    return report
