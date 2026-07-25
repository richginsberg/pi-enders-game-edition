"""`dnc-burst` CLI — plan, template, gate, up/down, reap.

Everything that spends money is opt-in and prints the projected cost first.
"""

from __future__ import annotations

import asyncio
import json

import typer

from .drivers import get_driver
from .lifecycle import BurstManager
from .policy import Demand, should_burst
from .profiles import DEFAULT_MODEL, GPUS, MODELS, rank_gpus
from .template import SWITCHES, render

app = typer.Typer(add_completion=False, help="Rent a GPU, saturate it, give it back.")


@app.command()
def models() -> None:
    """List servable models and their KV geometry."""
    for m in MODELS.values():
        kind = f"MoE {m.active_params_b:g}B active" if m.is_moe else "dense"
        typer.echo(
            f"{m.name:20s} {m.total_params_b:>5g}B {kind:18s} "
            f"SWE-bench {m.swe_bench_verified or '—':>5} "
            f"KV {m.kv_bytes_per_token(1)//1024:>3d} KiB/tok  quants: {','.join(sorted(m.quants))}")


@app.command()
def capacity(model: str = DEFAULT_MODEL, quant: str = "autoround",
             context: int = 262144, min_seqs: int = 8) -> None:
    """Cheapest cards that serve this model at `context` with >= min_seqs concurrency."""
    plans = rank_gpus(model, quant, context=context, min_seqs=min_seqs)
    if not plans:
        typer.secho(f"no GPU serves {model}/{quant} at {context:,} with >={min_seqs} seqs",
                    fg="red"); raise typer.Exit(1)
    typer.echo(f"{'gpu':14s}{'$/hr':>7s}{'VRAM':>7s}{'max-num-seqs':>14s}{'KV pool':>10s}")
    for p in plans:
        g = GPUS[p.gpu]
        typer.echo(f"{p.gpu:14s}{g.usd_per_hr:>7.2f}{g.vram_gb:>6g}G"
                   f"{p.max_num_seqs:>14d}{p.kv_pool_gib:>9.1f}G")


@app.command()
def template(model: str = DEFAULT_MODEL, quant: str = "autoround",
             gpu: str = "rtxpro6000", context: int = 262144,
             profile: str = "saturation", as_json: bool = False) -> None:
    """Render the injectable vLLM launch template for a card."""
    try:
        t = render(model, quant, gpu, context=context, profile=profile)
    except ValueError as e:
        typer.secho(str(e), fg="red"); raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(t.as_dict(), indent=2)); return
    typer.echo(f"# {t.model} / {t.quant} on {t.gpu} ({profile})")
    typer.echo(f"# KV {t.plan.kv_bytes_per_token//1024} KiB/tok · pool {t.plan.kv_pool_gib} GiB "
               f"· {t.plan.token_pool:,} tokens · max-num-seqs {t.plan.max_num_seqs}")
    for k, v in t.env.items():
        typer.echo(f"export {k}={v}")
    typer.echo(t.command)
    for w in t.warnings:
        typer.secho(f"! {w}", fg="yellow")


@app.command()
def switches() -> None:
    """Explain every switch the templates emit."""
    for k, v in SWITCHES.items():
        typer.secho(k, fg="cyan", bold=True)
        typer.echo(f"    {v}\n")


@app.command()
def gate(tokens: int = typer.Option(..., help="projected OUTPUT tokens"),
         hours: float = typer.Option(..., help="projected duration"),
         tok_s: float = typer.Option(..., help="expected aggregate tok/s"),
         fallback_usd_per_m: float = 10.0, gpu: str = "rtxpro6000",
         saturation: float = 1.0) -> None:
    """Should we rent? Prints the determination and the money either way."""
    from .policy import Economics
    from .api import policy_from_env
    pol = policy_from_env()
    econ = Economics(GPUS[gpu].usd_per_hr, tok_s, fallback_usd_per_m)
    d = should_burst(Demand(tokens, hours, saturation), econ, pol)
    colour = "green" if d.ok else "yellow"
    typer.secho(f"{d.verdict.upper()}: {d.reason}", fg=colour)
    typer.echo(f"  effective ${econ.effective_usd_per_m():.3f}/M out "
               f"(break-even {econ.breakeven_tok_s():.0f} tok/s)")
    typer.echo(f"  rent ${d.projected_cost_usd:.2f}  vs fallback ${d.fallback_cost_usd:.2f}"
               f"  -> saves ${d.savings_usd:.2f}")
    if not pol.enabled:
        typer.secho("  (burst disabled — set BURST_ENABLED=true to allow)", fg="yellow")


def _mgr() -> BurstManager:
    from .api import policy_from_env
    import os
    return BurstManager(driver=get_driver(os.environ.get("BURST_PROVIDER", "runpod")),
                        policy=policy_from_env())


@app.command()
def up(model: str = DEFAULT_MODEL, quant: str = "autoround", gpu: str = None,
       context: int = 262144, replicas: int = 1) -> None:
    """Rent and serve. Streams progress; registers only once /v1/models answers."""
    mgr = _mgr()
    if not mgr.policy.enabled:
        typer.secho("burst is not enabled (BURST_ENABLED=true)", fg="red"); raise typer.Exit(1)

    async def run():
        async for ev in mgr.scale_up(model=model, quant=quant, gpu_key=gpu,
                                     context=context, replicas=replicas):
            typer.echo(json.dumps(ev, default=str))
    asyncio.run(run())


@app.command()
def down(node_id: str = None) -> None:
    """Release one node (or all): deregister, drain, terminate."""
    mgr = _mgr()

    async def run():
        for n in ([mgr.nodes[node_id]] if node_id else list(mgr.nodes.values())):
            typer.echo(json.dumps(await mgr.release(n, "cli"), default=str))
    asyncio.run(run())


@app.command()
def reap() -> None:
    """One reaper pass — idle TTL, hard lifetime, and orphan detection."""
    mgr = _mgr()

    async def run():
        typer.echo(json.dumps(await mgr.reap(), default=str, indent=2))
        orphans = mgr.reconcile()
        if orphans:
            typer.secho(f"! {len(orphans)} orphaned node(s) not in the registry: "
                        f"{[o.node_id for o in orphans]}", fg="red")
    asyncio.run(run())


@app.command()
def status() -> None:
    """Live nodes, elapsed time and accrued cost."""
    typer.echo(json.dumps(_mgr().status(), indent=2, default=str))


if __name__ == "__main__":
    app()
