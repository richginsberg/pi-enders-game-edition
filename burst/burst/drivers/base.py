"""Provider driver contract.

A driver knows how to rent a machine, attach a persistent weights volume, and give it back.
Everything above this layer (policy, lifecycle, templates) is provider-agnostic.

`Phase.ZERO_GPU` is a first-class state, not an error: RunPod documents that starting a
stopped pod "may be allocated zero GPUs if capacity has changed" — you get a pod that is
reachable and still billing storage but cannot serve. Treating that as "running" is how you
end up paying for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Phase(str, Enum):
    CREATING = "creating"        # provider accepted the request
    PROVISIONING = "provisioning"  # machine exists, container not up
    LOADING = "loading"          # container up, model still loading
    SERVING = "serving"          # /v1/models answered — only now is it routable
    ZERO_GPU = "zero_gpu"        # allocated without a GPU; unusable, still billing
    GONE = "gone"                # terminated or vanished (preemption)
    ERROR = "error"


TERMINAL = {Phase.SERVING, Phase.ZERO_GPU, Phase.GONE, Phase.ERROR}


@dataclass(frozen=True)
class Offer:
    """A rentable machine the provider currently has."""
    offer_id: str
    gpu_key: str
    gpu_name: str
    vram_gb: float
    usd_per_hr: float
    provider: str
    available: int = 1


@dataclass
class BurstNode:
    """A rented machine we are responsible for."""
    node_id: str
    provider: str
    instance_id: str
    gpu_key: str
    usd_per_hr: float
    model: str
    quant: str
    volume_id: str | None = None
    endpoint: str | None = None       # OpenAI-compatible base, e.g. https://…/v1
    phase: Phase = Phase.CREATING
    created_at: float = 0.0
    last_request_at: float = 0.0
    registered: bool = False          # is it in the gateway right now?
    meta: dict = field(default_factory=dict)


class BurstDriver(Protocol):
    """Minimal surface a provider must implement."""

    name: str

    def search(self, *, min_vram_gb: float, gpu_key: str | None = None) -> list[Offer]:
        """Offers with stock, cheapest first."""

    def ensure_volume(self, name: str, size_gb: int) -> str:
        """Create-or-find the persistent weights volume. Returns its id.

        This is what makes hour-scale sessions economic: weights survive termination, so a
        redeploy is ~seconds instead of minutes, and you are never forced to *stop* a pod
        (which on RunPod doubles the disk rate and releases the GPU with no reservation)."""

    def create(self, offer: Offer, *, image: str, args: list[str], env: dict[str, str],
               volume_id: str | None, port: int = 8000) -> BurstNode:
        """Rent the machine and start the server. Returns immediately; poll with status()."""

    def status(self, node: BurstNode) -> Phase:
        """Provider-side phase. Never returns SERVING — only a real /v1/models probe can."""

    def endpoint(self, node: BurstNode) -> str | None:
        """Stable OpenAI-compatible base URL, preferring a proxy hostname over a raw IP
        (Community pods change IP and external ports on restart/migration)."""

    def terminate(self, node: BurstNode) -> None:
        """Give the machine back. Prefer this to 'stop' — see the driver docstrings."""

    def list_nodes(self) -> list[BurstNode]:
        """Everything we own at this provider, for startup reconciliation. Anything the
        registry doesn't know about is a leak and must be reaped."""

    def destroy_volume(self, volume_id: str) -> None:
        """Delete a weights volume (it bills monthly whether or not a pod exists)."""
