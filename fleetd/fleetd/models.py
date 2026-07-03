"""Core data model. One SQLite database, four tables: hosts, deployments, tasks, events."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class StrEnum(str, Enum):
    """str+Enum base, compatible with Python 3.10 (enum.StrEnum is 3.11+)."""

    def __str__(self) -> str:  # match enum.StrEnum behavior
        return self.value


class Squad(StrEnum):
    S0_FRONTIER = "s0"  # remote APIs (GLM, Grok, ...)
    S1_HEAVY = "s1"     # 8x/4x RTX 3090 rigs, big MoE GGUF
    S2_MID = "s2"       # single 3090s, V100
    S3_WIDE = "s3"      # Pascal, BC-250 swarm


class ServerKind(StrEnum):
    VLLM = "vllm"
    LLAMACPP = "llamacpp"
    API = "api"  # remote provider, no host


class Management(StrEnum):
    """Who owns the deployment's lifecycle."""

    DOCKER = "docker"      # standard harness-managed container: full lifecycle
    ADOPTED = "adopted"    # pre-existing server discovered on the host: monitor-only,
                           # never upgraded/restarted by plays; drift is reported not fixed
    MIGRATING = "migrating"  # adopted server with a managed replacement mid-cutover


class GpuArch(StrEnum):
    PASCAL = "pascal"        # P102-100, CMP 210-100 (sm_61)
    VOLTA = "volta"          # V100 (sm_70)
    AMPERE = "ampere"        # RTX 3090 (sm_86)
    RDNA2_BC250 = "bc250"    # AMD BC-250 (gfx1013, ROCm/Vulkan)


class Host(BaseModel):
    id: str  # short slug, e.g. "rig-3090-a"
    address: str
    ssh_user: str = "root"
    ssh_port: int = 22
    squad: Squad
    gpu_arch: GpuArch | None = None  # None for API pseudo-hosts
    gpu_count: int = 0
    vram_gb_per_gpu: float = 0
    nic_gbps: float = 1.0
    labels: dict[str, str] = Field(default_factory=dict)


class Deployment(BaseModel):
    id: str
    host_id: str
    server: ServerKind
    server_version: str  # image tag or release, e.g. "vllm/vllm-openai:v0.8.4"
    model_id: str        # e.g. "stepfun-ai/Step-3.5-Flash-GGUF"
    quant: str | None = None
    context_window: int = 32768
    port: int = 8000
    extra_args: list[str] = Field(default_factory=list)
    status: str = "unknown"  # unknown|deploying|healthy|unhealthy|stopped
    management: Management = Management.DOCKER
    # Discovery facts for adopted servers: raw process cmdline, binary path,
    # unit name (if systemd), detected version. Basis for migration diffs.
    discovered: dict[str, str] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    """One relentless-harness task: DoD checklist + engagement info."""

    id: str
    title: str
    definition_of_done: list[str]
    done_items: list[bool] = Field(default_factory=list)
    status: str = "running"  # running|blocked|done|failed|escalated
    iteration: int = 0
    engaged_hosts: list[str] = Field(default_factory=list)
    engaged_models: list[str] = Field(default_factory=list)
    tmux_session: str | None = None
    started_at: float = 0
    updated_at: float = 0
