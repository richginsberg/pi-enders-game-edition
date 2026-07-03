"""fleetd CLI: `fleetd serve`, `fleetd hosts add ...` etc."""

from __future__ import annotations

import typer
import uvicorn

app = typer.Typer(no_args_is_help=True, help="Divide-and-conquer fleet daemon")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 7431) -> None:
    """Run the fleetd API server."""
    uvicorn.run("fleetd.api:app", host=host, port=port)


@app.command()
def preflight(address: str, user: str = "root") -> None:
    """Check a new host: SSH, Docker, GPU visibility."""
    import asyncio

    from . import plays
    from .models import Host, Squad

    h = Host(id="preflight", address=address, ssh_user=user, squad=Squad.S3_WIDE)
    results = asyncio.run(plays.preflight(h))
    for name, r in results.items():
        status = "ok" if r["ok"] else "FAIL"
        typer.echo(f"{name}: {status} — {r['output']}")


if __name__ == "__main__":
    app()
