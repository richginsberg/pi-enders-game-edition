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
    assert "--device=/dev/kfd" in cmd
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
