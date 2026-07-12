"""IaC plays: idempotent SSH/Docker actions against a host (Marionette-style push).

Every play converges: each step checks current state before acting, so re-running
is safe. Pure command-rendering lives in module functions (unit-testable without
SSH); the async plays do the remote I/O via `run()`.

Lifecycle rules:
- management=docker deployments: full lifecycle (deploy/upgrade/stop).
- management=adopted deployments: NEVER touched by plays; monitor-only.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Callable
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

# Throughput floor (tok/s) below which a BC-250 node is presumed CPU-bound rather than
# serving on the GPU. A healthy BC-250 does ~56 tok/s; the Ubuntu-kernel CPU-fallback trap
# measured ~12 tok/s. 30 sits with wide margin on both sides so a slow-but-GPU node still
# passes and a fast-CPU fluke can't. Only enforced for BC-250 (other arches vary too widely).
GPU_TPS_FLOOR_BC250 = 30.0


def image_ref(host: Host, dep: Deployment) -> str:
    assert host.gpu_arch is not None
    repo = IMAGES[(dep.server, host.gpu_arch)]
    return f"{repo}:{dep.server_version}"


def container_name(dep: Deployment) -> str:
    return f"dnc-{dep.id}"


def model_ref(dep: Deployment) -> str:
    """In-container path to the model file/dir the server should load."""
    return dep.model_path or f"{MODELS_DIR}/{dep.model_id}"


def server_args(host: Host, dep: Deployment) -> list[str]:
    """Inference-server CLI args from the deployment spec."""
    if dep.server == ServerKind.LLAMACPP:
        args = [
            "-m", model_ref(dep),
            "--host", "0.0.0.0",
            "--port", str(dep.port),
            "-c", str(dep.context_window),
            "-ngl", "999",
            "--alias", dep.model_id.split("/")[-1].lower(),
        ]
        if host.gpu_count > 1:
            args += ["--split-mode", "layer"]
        if host.gpu_arch == GpuArch.RDNA2_BC250:
            # BC-250 defaults. `--flash-attn on` is required for the q8_0 V-cache.
            # `--jinja` is REQUIRED for reasoning models (e.g. Qwen3.6): without it
            # llama.cpp never applies the model's end-of-turn token, so it never stops
            # and runs to the context limit (measured: 50k+ tokens, wedges the slot).
            # `--parallel 1` is the accurate fleet scenario (concurrency spreads across
            # ~24 nodes, not within one). Generation caps bound any runaway loop.
            args += [
                "--flash-attn", "on",
                "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                "--jinja",
                "--parallel", "1",
                "-n", "8192",
                "--repeat-penalty", "1.1", "--repeat-last-n", "256",
                # Cap thinking (not total): forces the model to stop reasoning and emit
                # an answer, so a hard prompt can't burn the whole turn thinking and
                # return empty content. No-op for non-reasoning models. Tune per workload.
                "--reasoning-budget", "4096",
            ]
    elif dep.server == ServerKind.VLLM:
        args = [
            "--model", model_ref(dep),
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
    # BC-250 uses the Vulkan/RADV backend (NOT ROCm /dev/kfd). It needs BOTH DRM nodes
    # (renderD128 + card1) with the host's supplementary groups, AND the host's
    # BC-250-PATCHED RADV driver bind-mounted over the container's stock one — stock
    # upstream Mesa reports the chip as "unknown (143,132)" and winsys init fails.
    # Verified on the node: without the patched driver you get llvmpipe (CPU) and the
    # 3.5GiB host thrashes; with it, ~79 tok/s on the GPU. (host RADV path is
    # host-specific; RADV_DRIVER overridable.)
    if host.gpu_arch == GpuArch.RDNA2_BC250:
        # Pass the whole /dev/dri (DRM card node numbering — card0/card1 — is NOT stable
        # across reboots; the render node is). keep-groups carries the host render/video
        # groups. The BC-250-patched RADV driver is baked into the image (deploy/bc250),
        # so no host driver bind-mount is needed.
        gpu_flag = "--device /dev/dri --group-add keep-groups"
    else:
        gpu_flag = "--gpus all"
    # Mount the shared model store, or the existing model's own dir when reusing an
    # on-disk model (migration). Same path inside and out so model_ref() resolves.
    mount_dir = posixpath.dirname(dep.model_path) if dep.model_path else MODELS_DIR
    parts = [
        f"docker rm -f {name} >/dev/null 2>&1 || true;",
        "docker run -d",
        f"--name {name}",
        "--restart unless-stopped",
        gpu_flag,
        f"-v {shlex.quote(mount_dir)}:{shlex.quote(mount_dir)}:ro",
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
    # Optional sink invoked with each step dict as it happens (for live streaming).
    # Sync callback, called from the play's event loop — e.g. queue.put_nowait.
    on_step: Callable[[dict], None] | None = None

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        entry = {"name": name, "ok": ok, "detail": detail.strip()[-2000:]}
        self.steps.append(entry)
        self.ok = self.ok and ok
        if self.on_step is not None:
            self.on_step(entry)


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
    # Migration reuses a model already on disk: verify it's there, never download.
    if dep.model_path:
        exists = await run(host, f"test -e {shlex.quote(dep.model_path)}")
        ok = exists.exit_status == 0
        report.step("model", ok, "reusing on-disk model" if ok else f"missing {dep.model_path}")
        return
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


def inference_probe_command(dep: Deployment, container: str) -> str:
    """Render a remote snippet that fires ONE real completion and emits two marker lines.

    A passing /health only proves the port answers — it does NOT prove inference runs on
    the GPU. The classic trap (BC-250 on an Ubuntu kernel that never gave RADV a working
    gfx1013 compute device): llama.cpp silently falls back to CPU, serves ~5x slower, yet
    /health is green. So we assert the two fingerprints that separate GPU from CPU-bound:
      DNC_OFFLOAD=<gpu|cpu|unknown>  from the server's OWN startup log (offloaded layers vs
                                     an llvmpipe/CPU Vulkan device)
      DNC_TPS=<float>               throughput straight from llama.cpp's timings (no client
                                     math): predicted_per_second on a fixed tiny completion
    Both are emitted on their own line for robust parsing; TPS defaults to 0 on any failure.
    """
    url = f"http://localhost:{dep.port}/completion"
    body = '{"prompt":"Reply with the single word: ok.","n_predict":48,"stream":false,"cache_prompt":false}'
    py = (
        "import sys,json;"
        "d=json.load(sys.stdin);"
        "print('DNC_TPS=%.1f' % d.get('timings',{}).get('predicted_per_second',0))"
    )
    return (
        # (1) offload signal from the container's own boot log — the same RADV llama.cpp uses.
        f"log=$(docker logs {shlex.quote(container)} 2>&1 | "
        f'grep -iE "llvmpipe|offloaded.*layers to GPU|Vulkan0|RADV|gfx1013|CPU_TYPE" | tail -40);'
        f'if echo "$log" | grep -qiE "llvmpipe|PHYSICAL_DEVICE_TYPE_CPU"; then echo DNC_OFFLOAD=cpu;'
        f'elif echo "$log" | grep -qiE "offloaded .*layers to GPU|Vulkan0|RADV|gfx1013"; then echo DNC_OFFLOAD=gpu;'
        f"else echo DNC_OFFLOAD=unknown; fi;"
        # (2) throughput from llama.cpp's own reported timings on a fixed tiny completion.
        f"curl -sf {url} -H 'Content-Type: application/json' -d {shlex.quote(body)} "
        f"| python3 -c {shlex.quote(py)} || echo DNC_TPS=0"
    )


def _marker(out: str, key: str) -> str:
    """Last value of a `KEY=value` marker line (last wins if the probe echoed twice)."""
    val = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            val = line.split("=", 1)[1]
    return val


async def assert_gpu_inference(
    host: Host, dep: Deployment, report: PlayReport, *, min_tok_s: float | None = None
) -> None:
    """Post-health gate: fire one completion and fail if the node is serving CPU-bound.

    llama.cpp-specific (the probe reads llama.cpp's /completion timings). The throughput
    floor is enforced only for BC-250 (the arch with the CPU-fallback trap and a known-good
    baseline); elsewhere we still fail on an explicit CPU/llvmpipe offload marker but don't
    impose a t/s floor. Sets report.ok=False on failure so fleetd flags the node instead of
    registering a dud that quietly serves ~5x slow.
    """
    if dep.server != ServerKind.LLAMACPP:
        return  # probe reads llama.cpp timings; skip for other server kinds
    floor = (
        min_tok_s
        if min_tok_s is not None
        else (GPU_TPS_FLOOR_BC250 if host.gpu_arch == GpuArch.RDNA2_BC250 else 0.0)
    )
    proc = await run(host, inference_probe_command(dep, container_name(dep)))
    out = proc.stdout or ""
    offload = _marker(out, "DNC_OFFLOAD") or "unknown"
    try:
        tps = float(_marker(out, "DNC_TPS") or 0)
    except ValueError:
        tps = 0.0

    if offload == "cpu":
        report.step("gpu_inference", False,
                    f"CPU FALLBACK: llama.cpp on CPU/llvmpipe (the Ubuntu-kernel trap), "
                    f"{tps:.1f} tok/s — not fit to serve")
        return
    if tps <= 0:
        report.step("gpu_inference", False,
                    f"probe produced no throughput (offload={offload}); completion failed or "
                    f"timings missing")
        return
    if floor and tps < floor:
        report.step("gpu_inference", False,
                    f"throughput {tps:.1f} < floor {floor:g} tok/s (offload={offload}) — node is "
                    f"CPU-bound, not serving on the GPU")
        return
    report.step("gpu_inference", True, f"{tps:.1f} tok/s, offload={offload} (floor {floor:g})")


async def deploy(host: Host, dep: Deployment, *, on_step: Callable[[dict], None] | None = None) -> PlayReport:
    """Deploy or upgrade an inference server container. Idempotent by container name."""
    report = PlayReport(play="deploy", host_id=host.id, on_step=on_step)
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
    if not report.ok:
        return report  # never serving — the inference probe would just time out

    # Health is green, but green only means the port answers. Assert the node actually
    # serves on the GPU (not the CPU-fallback trap) before it's declared deploy-ok.
    await assert_gpu_inference(host, dep, report)
    # TODO(M3, task #8/#10): on success, register with LiteLLM (fleetd litellm_sync)
    return report


async def stop(host: Host, dep: Deployment, *, on_step: Callable[[dict], None] | None = None) -> PlayReport:
    report = PlayReport(play="stop", host_id=host.id, on_step=on_step)
    if dep.management != Management.DOCKER:
        report.step("guard", False, "adopted servers are never stopped by plays")
        return report
    proc = await run(host, f"docker rm -f {container_name(dep)}")
    report.step("stop", proc.exit_status == 0, proc.stderr or "")
    return report


# -- migration: adopted server -> standard managed Docker deployment ----------------
def plan_migration(adopted: Deployment, *, new_port: int, target_version: str = "latest") -> Deployment:
    """Derive an equivalent managed Docker deployment from an adopted server's facts.

    Reuses the model already on disk (model_path) so no re-download is needed, and
    binds to a fresh port so the standard container can run side-by-side with the
    old server until cutover verifies.
    """
    return Deployment(
        id=f"mig-{adopted.host_id}-{new_port}",
        host_id=adopted.host_id,
        server=adopted.server,
        server_version=target_version,
        model_id=adopted.model_id,
        model_path=adopted.model_path or adopted.discovered.get("model"),
        quant=adopted.quant,
        context_window=adopted.context_window,
        port=new_port,
        management=Management.DOCKER,
    )


def migration_diff(host: Host, adopted: Deployment, proposed: Deployment) -> list[dict]:
    """Field-by-field before/after for the TUI preview (task #10)."""
    d = adopted.discovered
    rows = [
        ("runner", d.get("runner", "bare"), "docker (--restart unless-stopped)"),
        ("image", d.get("binary", "custom build"), image_ref(host, proposed)),
        ("version", d.get("version", "unknown"), proposed.server_version),
        ("port", str(adopted.port), str(proposed.port)),
        ("model", proposed.model_path or proposed.model_id, "(reused, no re-download)"),
        ("server_cmd", d.get("cmdline", ""), " ".join(server_args(host, proposed))),
    ]
    return [{"field": f, "from": a, "to": b} for f, a, b in rows if str(a) != str(b)]


def stop_adopted_command(adopted: Deployment) -> str:
    """Render the command to stop the OLD adopted server, by how it's kept alive.

    This is the one sanctioned place a play touches an adopted process, and only
    mid-migration after the managed replacement is verified healthy.
    """
    d = adopted.discovered
    runner, unit, pid = d.get("runner", "bare"), d.get("unit"), d.get("pid")
    if runner == "docker" and unit:
        return f"docker rm -f {shlex.quote(unit)}"
    if runner == "systemd" and unit:
        return f"systemctl stop {shlex.quote(unit)}"
    if pid:
        return f"kill {int(pid)}"
    raise ValueError(f"cannot stop adopted server {adopted.id}: no unit or pid recorded")


async def stop_adopted(
    host: Host, adopted: Deployment, *, on_step: Callable[[dict], None] | None = None
) -> PlayReport:
    report = PlayReport(play="stop_adopted", host_id=host.id, on_step=on_step)
    if adopted.management not in (Management.ADOPTED, Management.MIGRATING):
        report.step("guard", False, f"stop_adopted only for adopted/migrating (got {adopted.management})")
        return report
    proc = await run(host, stop_adopted_command(adopted))
    report.step("stop_adopted", proc.exit_status == 0, proc.stderr or "")
    return report


async def migrate(
    host: Host,
    adopted: Deployment,
    *,
    new_port: int,
    target_version: str = "latest",
    on_step: Callable[[dict], None] | None = None,
) -> tuple[PlayReport, Deployment]:
    """Cutover an adopted server to a standard managed deployment, side-by-side.

    Deploy the standard container on a fresh port, verify it's healthy, and only
    then stop the old server. Returns the report and the (now-live) managed
    Deployment so the caller can persist it and flip LiteLLM registration.
    """
    report = PlayReport(play="migrate", host_id=host.id, on_step=on_step)
    if adopted.management not in (Management.ADOPTED, Management.MIGRATING):
        report.step("guard", False, f"migrate expects an adopted server (got {adopted.management})")
        return report, adopted

    proposed = plan_migration(adopted, new_port=new_port, target_version=target_version)

    # Bring up the standard container beside the old one. Pass the sink so its steps
    # stream live; extend() then copies them into this report without re-emitting.
    deploy_report = await deploy(host, proposed, on_step=on_step)
    report.steps.extend(deploy_report.steps)
    report.ok = report.ok and deploy_report.ok
    if not report.ok:
        report.step("cutover", False, "aborting: managed replacement not healthy; old server left running")
        return report, adopted

    # Replacement verified healthy -> stop the old server. (LiteLLM flip is the
    # caller's job via litellm_sync — TODO M3.)
    stop_report = await stop_adopted(host, adopted, on_step=on_step)
    report.steps.extend(stop_report.steps)
    report.ok = report.ok and stop_report.ok
    report.step("cutover", report.ok, "migrated to managed deployment" if report.ok else "old server stop failed")
    return report, proposed
