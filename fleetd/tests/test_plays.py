import pytest

from fleetd.models import Deployment, GpuArch, Host, Management, ServerKind, Squad
from fleetd.plays import container_name, docker_run_command, image_ref, server_args


AMPERE_RIG = Host(
    id="rig-3090-a", address="10.0.0.21", squad=Squad.S1_HEAVY,
    gpu_arch=GpuArch.AMPERE, gpu_count=4, vram_gb_per_gpu=24,
)
V100 = Host(
    id="v100-a", address="10.0.0.31", squad=Squad.S2_MID,
    gpu_arch=GpuArch.VOLTA, gpu_count=1, vram_gb_per_gpu=16,
)
BC250 = Host(
    id="bc250-01", address="10.0.0.101", squad=Squad.S3_WIDE,
    gpu_arch=GpuArch.RDNA2_BC250, gpu_count=1, vram_gb_per_gpu=16,
)


def llamacpp_dep(host_id: str) -> Deployment:
    return Deployment(
        id=f"{host_id}-step35", host_id=host_id, server=ServerKind.LLAMACPP,
        server_version="server-cuda-b4823",
        model_id="stepfun-ai/Step-3.5-Flash-GGUF/Step-3.5-Flash-Q3_K_M.gguf",
        context_window=65536, port=8080,
    )


def vllm_dep(host_id: str) -> Deployment:
    return Deployment(
        id=f"{host_id}-qwen8b", host_id=host_id, server=ServerKind.VLLM,
        server_version="v0.8.4", model_id="Qwen/Qwen3-8B",
        context_window=32768, port=8000, quant=None,
    )


def test_image_ref_pins_version():
    assert image_ref(AMPERE_RIG, llamacpp_dep("rig-3090-a")) == "ghcr.io/ggml-org/llama.cpp:server-cuda-b4823"
    assert image_ref(V100, vllm_dep("v100-a")) == "vllm/vllm-openai:v0.8.4"


def test_llamacpp_args_multi_gpu():
    args = server_args(AMPERE_RIG, llamacpp_dep("rig-3090-a"))
    assert "-c" in args and args[args.index("-c") + 1] == "65536"
    assert "--split-mode" in args  # 4 GPUs -> layer split
    assert args[args.index("--alias") + 1] == "step-3.5-flash-q3_k_m.gguf"


def test_vllm_args_single_gpu_no_tp():
    args = server_args(V100, vllm_dep("v100-a"))
    assert "--tensor-parallel-size" not in args
    assert args[args.index("--max-model-len") + 1] == "32768"


def test_docker_run_is_idempotent_and_labeled():
    cmd = docker_run_command(AMPERE_RIG, llamacpp_dep("rig-3090-a"))
    assert cmd.startswith("docker rm -f dnc-rig-3090-a-step35 ")  # replace, not duplicate
    assert "--restart unless-stopped" in cmd
    assert "--label dnc.managed=true" in cmd
    assert "--gpus all" in cmd


def test_bc250_uses_amd_devices_and_custom_image():
    dep = llamacpp_dep("bc250-01")
    dep.server_version = "latest"
    cmd = docker_run_command(BC250, dep)
    assert "--device /dev/dri" in cmd  # Vulkan/RADV render node, not ROCm /dev/kfd
    assert "--group-add render" in cmd
    assert "--gpus all" not in cmd
    assert "dnc/llamacpp-bc250:latest" in cmd


def test_extra_args_appended():
    dep = llamacpp_dep("rig-3090-a")
    dep.extra_args = ["--flash-attn"]
    assert server_args(AMPERE_RIG, dep)[-1] == "--flash-attn"


@pytest.mark.asyncio
async def test_deploy_refuses_adopted():
    from fleetd.plays import deploy

    dep = llamacpp_dep("rig-3090-a")
    dep.management = Management.ADOPTED
    report = await deploy(AMPERE_RIG, dep)  # returns before any SSH happens
    assert report.ok is False
    assert report.steps[0]["name"] == "guard"


def test_container_name():
    assert container_name(vllm_dep("v100-a")) == "dnc-v100-a-qwen8b"


# -- migration ----------------------------------------------------------------------
def adopted_step(runner="bare", **discovered) -> Deployment:
    """An adopted Step-3.5-Flash server, as discovery would catalog it."""
    d = {
        "runner": runner, "pid": "4242", "binary": "/opt/llama.cpp/llama-server",
        "cmdline": "/opt/llama.cpp/llama-server -m /srv/models/Step-3.5-Flash-Q3_K_M.gguf --port 5000",
        "model": "/srv/models/Step-3.5-Flash-Q3_K_M.gguf", "version": "b4823",
        **discovered,
    }
    return Deployment(
        id="adopted-rig-3090-a-5000", host_id="rig-3090-a", server=ServerKind.LLAMACPP,
        server_version="b4823", model_id="step-3.5-flash-q3_k_m.gguf",
        model_path="/srv/models/Step-3.5-Flash-Q3_K_M.gguf",
        context_window=65536, port=5000, management=Management.ADOPTED, discovered=d,
    )


def test_plan_migration_reuses_model_and_new_port():
    from fleetd.plays import plan_migration

    proposed = plan_migration(adopted_step(), new_port=8080, target_version="server-cuda-b4823")
    assert proposed.management == Management.DOCKER
    assert proposed.port == 8080  # side-by-side, not the adopted 5000
    assert proposed.model_path == "/srv/models/Step-3.5-Flash-Q3_K_M.gguf"  # reused, no download
    assert proposed.server_version == "server-cuda-b4823"


def test_migrated_container_mounts_existing_model_dir_not_models_dir():
    from fleetd.plays import docker_run_command, plan_migration, server_args

    proposed = plan_migration(adopted_step(), new_port=8080)
    cmd = docker_run_command(AMPERE_RIG, proposed)
    assert "-v /srv/models:/srv/models:ro" in cmd  # existing dir, not /opt/dnc/models
    assert "/opt/dnc/models" not in cmd
    args = server_args(AMPERE_RIG, proposed)
    assert args[args.index("-m") + 1] == "/srv/models/Step-3.5-Flash-Q3_K_M.gguf"


def test_migration_diff_reports_runner_and_port_change():
    from fleetd.plays import migration_diff, plan_migration

    adopted = adopted_step(runner="systemd", unit="stepfun.service")
    proposed = plan_migration(adopted, new_port=8080)
    fields = {r["field"]: r for r in migration_diff(AMPERE_RIG, adopted, proposed)}
    assert fields["runner"]["from"] == "systemd" and "docker" in fields["runner"]["to"]
    assert fields["port"]["from"] == "5000" and fields["port"]["to"] == "8080"


def test_stop_adopted_command_per_runner():
    from fleetd.plays import stop_adopted_command

    assert stop_adopted_command(adopted_step(runner="docker", unit="stepfun")) == "docker rm -f stepfun"
    assert stop_adopted_command(adopted_step(runner="systemd", unit="s.service")) == "systemctl stop s.service"
    assert stop_adopted_command(adopted_step(runner="bare")) == "kill 4242"


def test_stop_adopted_command_needs_handle():
    import pytest as _pytest

    from fleetd.plays import stop_adopted_command

    orphan = adopted_step(runner="bare")
    orphan.discovered.pop("pid")
    with _pytest.raises(ValueError):
        stop_adopted_command(orphan)


@pytest.mark.asyncio
async def test_migrate_refuses_managed_deployment():
    from fleetd.plays import migrate

    managed = llamacpp_dep("rig-3090-a")  # management defaults to DOCKER
    report, out = await migrate(AMPERE_RIG, managed, new_port=8080)  # returns before SSH
    assert report.ok is False
    assert report.steps[0]["name"] == "guard"
    assert out is managed  # unchanged


def test_playreport_sink_fires_per_step_live():
    from fleetd.plays import PlayReport

    seen = []
    r = PlayReport(play="x", host_id="h", on_step=seen.append)
    r.step("a", True)
    assert seen == [{"name": "a", "ok": True, "detail": ""}]  # emitted immediately, not at the end
    r.step("b", False, "boom")
    assert [s["name"] for s in seen] == ["a", "b"]
    assert seen is not r.steps and seen == r.steps  # sink got copies-in-order matching the report


@pytest.mark.asyncio
async def test_migrate_streams_substeps_through_sink(monkeypatch):
    """migrate() must relay its sub-plays' steps through the sink, in order, once each."""
    from fleetd import plays

    async def fake_deploy(host, dep, *, on_step=None):
        r = plays.PlayReport(play="deploy", host_id=host.id, on_step=on_step)
        r.step("pull", True)
        r.step("health", True)
        return r

    async def fake_stop_adopted(host, dep, *, on_step=None):
        r = plays.PlayReport(play="stop_adopted", host_id=host.id, on_step=on_step)
        r.step("stop_adopted", True)
        return r

    monkeypatch.setattr(plays, "deploy", fake_deploy)
    monkeypatch.setattr(plays, "stop_adopted", fake_stop_adopted)

    streamed = []
    report, _ = await plays.migrate(AMPERE_RIG, adopted_step(), new_port=8080, on_step=streamed.append)
    assert report.ok is True
    # sub-play steps arrive live, then migrate's own cutover — each exactly once
    assert [s["name"] for s in streamed] == ["pull", "health", "stop_adopted", "cutover"]


@pytest.mark.asyncio
async def test_migrate_leaves_old_server_running_if_replacement_unhealthy(monkeypatch):
    """Cutover must not stop the adopted server unless the managed one is healthy."""
    from fleetd import plays

    stopped = []

    async def fake_deploy(host, dep, *, on_step=None):
        r = plays.PlayReport(play="deploy", host_id=host.id, on_step=on_step)
        r.step("start", False, "boom")  # replacement failed to come up
        return r

    async def fake_stop_adopted(host, dep, *, on_step=None):
        stopped.append(dep.id)
        return plays.PlayReport(play="stop_adopted", host_id=host.id)

    monkeypatch.setattr(plays, "deploy", fake_deploy)
    monkeypatch.setattr(plays, "stop_adopted", fake_stop_adopted)

    report, out = await plays.migrate(AMPERE_RIG, adopted_step(), new_port=8080)
    assert report.ok is False
    assert stopped == []  # old server never touched
    assert out.management == Management.ADOPTED  # returned the still-live adopted server
