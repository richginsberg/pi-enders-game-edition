"""RunPod driver (pods, not Serverless).

Chosen first because two RunPod features decide hour-scale economics:

* **Network volumes** ($0.07/GB/mo) hold the weights independently of any pod, so a redeploy
  is seconds rather than a multi-minute HuggingFace pull — and they survive termination.
* **A stable proxy hostname** ``https://<pod-id>-<port>.proxy.runpod.net`` that does not
  churn, unlike Community Cloud public IPs and external ports.

We deliberately never call ``/stop``. RunPod docs: stopping doubles the volume-disk rate
($0.10 -> $0.20/GB/mo), clears the container disk, releases the GPU with no reservation, and
a later start "may be allocated zero GPUs if capacity has changed". Terminate against a
network volume instead.

HTTP is injectable so the whole driver is testable without an account or a cent of spend.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from .base import BurstNode, Offer, Phase

API = "https://rest.runpod.io/v1"
PROXY = "https://{pod_id}-{port}.proxy.runpod.net"

#: RunPod Community Cloud rates, verified 2026-07-25. Used to price offers when the API
#: response omits a rate; the API value always wins when present.
GPU_TYPE_IDS: dict[str, tuple[str, float, float]] = {
    # key -> (RunPod gpuTypeId, vram_gb, usd_per_hr)
    "rtx3090":    ("NVIDIA GeForce RTX 3090", 24, 0.22),
    "rtxa5000":   ("NVIDIA RTX A5000", 24, 0.16),
    "rtx4090":    ("NVIDIA GeForce RTX 4090", 24, 0.34),
    "l40":        ("NVIDIA L40", 48, 0.69),
    "rtx6000ada": ("NVIDIA RTX 6000 Ada Generation", 48, 0.74),
    "a100pcie":   ("NVIDIA A100 80GB PCIe", 80, 1.19),
    "rtx5090":    ("NVIDIA GeForce RTX 5090", 32, 0.69),
    "rtxpro6000": ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", 96, 1.69),
    "h100nvl":    ("NVIDIA H100 NVL", 94, 2.59),
}
_BY_TYPE_ID = {v[0]: k for k, v in GPU_TYPE_IDS.items()}

#: Tag written into every pod name so startup reconciliation can spot our leaks.
TAG = "dnc-burst"


class RunPodDriver:
    name = "runpod"

    def __init__(self, api_key: str | None = None, *,
                 request: Callable[..., Any] | None = None, datacenter: str | None = None):
        self.api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        self.datacenter = datacenter or os.environ.get("RUNPOD_DATACENTER")
        self._request = request or self._http

    # -- transport ---------------------------------------------------------------
    def _http(self, method: str, path: str, json: dict | None = None) -> Any:
        import httpx
        r = httpx.request(method, f"{API}{path}", json=json, timeout=60,
                          headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    # -- discovery ---------------------------------------------------------------
    def search(self, *, min_vram_gb: float, gpu_key: str | None = None) -> list[Offer]:
        keys = [gpu_key] if gpu_key else list(GPU_TYPE_IDS)
        offers = []
        for k in keys:
            if k not in GPU_TYPE_IDS:
                continue
            type_id, vram, rate = GPU_TYPE_IDS[k]
            if vram < min_vram_gb:
                continue
            offers.append(Offer(type_id, k, type_id, vram, rate, self.name))
        return sorted(offers, key=lambda o: o.usd_per_hr)

    # -- weights volume ----------------------------------------------------------
    def ensure_volume(self, name: str, size_gb: int) -> str:
        existing = self._request("GET", "/networkvolumes") or []
        for v in (existing.get("data", existing) if isinstance(existing, dict) else existing):
            if v.get("name") == name:
                return v["id"]
        body = {"name": name, "size": size_gb}
        if self.datacenter:
            body["dataCenterId"] = self.datacenter
        return self._request("POST", "/networkvolumes", body)["id"]

    def destroy_volume(self, volume_id: str) -> None:
        self._request("DELETE", f"/networkvolumes/{volume_id}")

    # -- lifecycle ---------------------------------------------------------------
    def create(self, offer: Offer, *, image: str, args: list[str], env: dict[str, str],
               volume_id: str | None, port: int = 8000) -> BurstNode:
        body: dict[str, Any] = {
            "name": f"{TAG}-{offer.gpu_key}-{int(time.time())}",
            "imageName": image,
            "gpuTypeIds": [offer.offer_id],
            "gpuCount": 1,
            "containerDiskInGb": 30,
            "ports": [f"{port}/http"],
            "env": dict(env),
            "dockerStartCmd": ["vllm", "serve", *args],
        }
        if volume_id:
            body["networkVolumeId"] = volume_id
            body["volumeMountPath"] = "/workspace"
        data = self._request("POST", "/pods", body)
        pod_id = data["id"]
        return BurstNode(
            node_id=pod_id, provider=self.name, instance_id=pod_id,
            gpu_key=offer.gpu_key, usd_per_hr=float(data.get("costPerHr") or offer.usd_per_hr),
            model="", quant="", volume_id=volume_id,
            endpoint=PROXY.format(pod_id=pod_id, port=port) + "/v1",
            phase=Phase.CREATING, created_at=time.time(),
            meta={"port": port},
        )

    def status(self, node: BurstNode) -> Phase:
        try:
            d = self._request("GET", f"/pods/{node.instance_id}")
        except Exception:  # noqa: BLE001 — a vanished pod is a normal event (preemption)
            return Phase.GONE
        if not d:
            return Phase.GONE
        # A pod with no GPU attached is RunPod's documented degraded-resume outcome.
        if int(d.get("gpuCount") or 0) < 1:
            return Phase.ZERO_GPU
        state = str(d.get("desiredStatus") or d.get("status") or "").upper()
        if state in ("TERMINATED", "EXITED"):
            return Phase.GONE
        if state == "RUNNING":
            return Phase.LOADING     # only a /v1/models probe may promote this to SERVING
        return Phase.PROVISIONING

    def endpoint(self, node: BurstNode) -> str | None:
        return node.endpoint

    def terminate(self, node: BurstNode) -> None:
        self._request("DELETE", f"/pods/{node.instance_id}")

    def list_nodes(self) -> list[BurstNode]:
        data = self._request("GET", "/pods") or []
        pods = data.get("data", data) if isinstance(data, dict) else data
        out = []
        for p in pods:
            if not str(p.get("name", "")).startswith(TAG):
                continue
            gpu_type = (p.get("machine") or {}).get("gpuTypeId") or ""
            out.append(BurstNode(
                node_id=p["id"], provider=self.name, instance_id=p["id"],
                gpu_key=_BY_TYPE_ID.get(gpu_type, gpu_type or "unknown"),
                usd_per_hr=float(p.get("costPerHr") or 0.0),
                model="", quant="", volume_id=p.get("networkVolumeId"),
                endpoint=PROXY.format(pod_id=p["id"], port=8000) + "/v1",
            ))
        return out
