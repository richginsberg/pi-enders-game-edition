"""Derive a stable partition key for the current repo / parent folder.

Priority (à la gentle-engram): git origin remote -> git toplevel dir -> cwd basename.
A partition scopes all reads/writes, so two checkouts of the same repo share memory
while unrelated projects stay isolated.

Pure normalization (`normalize_remote`, `partition_from`) is separated from the
git/fs probing (`detect_partition`) so it unit-tests without a repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?/(?P<path>.+)$")


def normalize_remote(url: str) -> str | None:
    """Normalize a git remote URL to a stable `host/org/repo` key (lowercased).

    Handles scp-like (git@github.com:org/repo.git), URL (https://…, ssh://…),
    ports, and trailing .git. Returns None if it can't be parsed.
    """
    url = url.strip()
    if not url:
        return None
    m = _URL_LIKE.match(url) or _SCP_LIKE.match(url)
    if not m:
        return None
    host = m.group("host")
    path = m.group("path").strip("/")
    path = re.sub(r"\.git$", "", path)
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def partition_from(origin_url: str | None, toplevel: str | None, cwd: str) -> str:
    """Pick a partition key from the available signals, most-specific first."""
    if origin_url:
        norm = normalize_remote(origin_url)
        if norm:
            return norm
    if toplevel:
        name = Path(toplevel).name
        if name:
            return name.lower()
    return Path(cwd).name.lower() or "default"


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def detect_partition(start: Path | None = None) -> str:
    """Probe git + filesystem for the current partition key."""
    cwd = (start or Path.cwd()).resolve()
    origin = _git(["config", "--get", "remote.origin.url"], cwd)
    toplevel = _git(["rev-parse", "--show-toplevel"], cwd)
    return partition_from(origin, toplevel, str(cwd))


def resolve_partition(partition: str | None = None, cwd: str | None = None) -> str:
    """Pick the partition for a request: explicit key wins, else detect from cwd.

    The context sidecar can serve many repos, so callers pass the working directory
    of the session that made the request rather than relying on the server's cwd.
    """
    if partition:
        return partition.lower()
    return detect_partition(Path(cwd) if cwd else None)
