"""Discovery of pre-existing inference servers on a host.

Detects servers the harness did not deploy — e.g. a custom local llama.cpp
compile serving Step-3.5-Flash — by scanning processes over SSH, classifying how
they are run (docker / systemd / bare), and extracting enough facts (binary,
flags, port, model, version) to either adopt them as-is or generate an
equivalent standard Docker deployment for migration.

Pure parsing logic lives in module functions so it is unit-testable without SSH;
`discover_host()` does the remote I/O.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from .models import Deployment, Host, Management, ServerKind

# Process name patterns → server kind. Order matters: check vllm before generic python.
PROCESS_SIGNATURES: list[tuple[str, ServerKind]] = [
    (r"(^|/)llama-server(\s|$)", ServerKind.LLAMACPP),
    (r"(^|/)server(\s|$).*--model.*\.gguf", ServerKind.LLAMACPP),  # old llama.cpp binary name
    (r"vllm(\.entrypoints|\s+serve)", ServerKind.VLLM),
    (r"python[0-9.]*\s.*-m\s+vllm", ServerKind.VLLM),
]

# Flags whose next token is the value we want, per fact key.
FLAG_FACTS = {
    "model": ["--model", "-m", "--model-path"],
    "port": ["--port"],
    "ctx": ["--ctx-size", "-c", "--max-model-len"],
    "ngl": ["--n-gpu-layers", "-ngl"],
    "tensor_parallel": ["--tensor-parallel-size", "-tp"],
    "alias": ["--alias", "--served-model-name"],
}

DEFAULT_PORTS = {ServerKind.LLAMACPP: 8080, ServerKind.VLLM: 8000}


@dataclass
class DiscoveredServer:
    kind: ServerKind
    cmdline: str
    pid: int
    binary: str
    management: Management = Management.ADOPTED
    runner: str = "bare"  # bare|systemd|docker — how the process is kept alive
    unit: str | None = None  # systemd unit or docker container name
    facts: dict[str, str] = field(default_factory=dict)

    @property
    def port(self) -> int:
        return int(self.facts.get("port", DEFAULT_PORTS[self.kind]))


def parse_process_line(line: str) -> DiscoveredServer | None:
    """Parse one `ps -eo pid,args` line into a DiscoveredServer, or None."""
    m = re.match(r"\s*(\d+)\s+(.+)", line)
    if not m:
        return None
    pid, cmdline = int(m.group(1)), m.group(2).strip()

    kind = next((k for pat, k in PROCESS_SIGNATURES if re.search(pat, cmdline)), None)
    if kind is None:
        return None

    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()

    # Last occurrence wins: `python -m vllm ... --model X` must resolve the model
    # to X, not to python's -m argument.
    facts: dict[str, str] = {}
    for key, flags in FLAG_FACTS.items():
        for i, tok in enumerate(tokens):
            if tok in flags and i + 1 < len(tokens):
                facts[key] = tokens[i + 1]
                continue
            for f in flags:
                if tok.startswith(f + "="):
                    facts[key] = tok.split("=", 1)[1]

    return DiscoveredServer(kind=kind, cmdline=cmdline, pid=pid, binary=tokens[0], facts=facts)


def model_slug(server: DiscoveredServer) -> str:
    """Best-effort model identifier from alias or model path."""
    if "alias" in server.facts:
        return server.facts["alias"]
    path = server.facts.get("model", "unknown")
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"(-\d{5}-of-\d{5})?\.(gguf|safetensors)$", "", name)


def to_deployment(host: Host, server: DiscoveredServer) -> Deployment:
    """Catalog entry for an adopted server. Monitor-only (management=adopted)."""
    return Deployment(
        id=f"adopted-{host.id}-{server.port}",
        host_id=host.id,
        server=server.kind,
        server_version=server.facts.get("version", "unknown"),
        model_id=model_slug(server),
        context_window=int(server.facts.get("ctx", 0)) or 32768,
        port=server.port,
        management=Management.ADOPTED,
        model_path=server.facts.get("model"),
        discovered={
            "cmdline": server.cmdline,
            "binary": server.binary,
            "runner": server.runner,
            "pid": str(server.pid),  # basis for stopping a bare process during migration
            **({"unit": server.unit} if server.unit else {}),
            **server.facts,
        },
    )


async def discover_host(host: Host) -> list[Deployment]:
    """SSH in, find inference-server processes, classify runner, catalog them."""
    from . import plays

    proc = await plays.run(host, "ps -eo pid,args --no-headers")
    servers = [
        s for line in (proc.stdout or "").splitlines()
        if (s := parse_process_line(line)) is not None
    ]

    for server in servers:
        # docker? cgroup of the pid mentions docker/containerd
        cg = await plays.run(host, f"cat /proc/{server.pid}/cgroup 2>/dev/null")
        if "docker" in (cg.stdout or "") or "containerd" in (cg.stdout or ""):
            server.runner = "docker"
            name = await plays.run(
                host,
                f"docker ps --filter status=running --format '{{{{.Names}}}} {{{{.ID}}}}' "
                f"| head -50",
            )
            server.unit = (name.stdout or "").split("\n")[0].split(" ")[0] or None
        else:
            # systemd? pid belongs to a service unit
            unit = await plays.run(
                host, f"systemctl status {server.pid} --no-pager 2>/dev/null | head -1"
            )
            um = re.match(r"[●x*]?\s*([\w@.-]+\.service)", (unit.stdout or "").strip())
            if um:
                server.runner = "systemd"
                server.unit = um.group(1)

        # server version from the binary itself (llama.cpp prints build info)
        if server.kind == ServerKind.LLAMACPP:
            v = await plays.run(host, f"{shlex.quote(server.binary)} --version 2>&1 | head -2")
            vm = re.search(r"(?:build|version)[:\s]+([\w.()-]+)", v.stdout or "")
            if vm:
                server.facts["version"] = vm.group(1)

    return [to_deployment(host, s) for s in servers]
