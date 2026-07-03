/**
 * /deploy — multi-step wizard to stand up, adopt, or migrate an inference server.
 *
 * Composed from Pi's UI primitives (select/confirm/input); each step returns
 * undefined on cancel, which aborts the wizard cleanly. Every mutation goes
 * through a fleetd play, so this file holds no SSH/Docker logic of its own.
 *
 * Flow:
 *   pick host (or add + preflight a new one)
 *     -> discover existing servers
 *        -> existing found: adopt-as-is (monitor-only) | migrate-to-standard
 *        -> none / "new model": fresh deploy (server, version, model, quant, ctx, port)
 */
import type { ExtensionAPI, ExtensionUIContext } from "@earendil-works/pi-coding-agent";
import { fleetdGet, fleetdSend, fleetdStream } from "./config.js";

interface HostRow {
  id: string;
  address: string;
  squad: string;
  gpu_arch: string | null;
  gpu_count: number;
}

interface DeploymentRow {
  id: string;
  host_id: string;
  server: string;
  model_id: string;
  port: number;
  status: string;
  management: string;
}

interface PlayStep {
  name: string;
  ok: boolean;
  detail: string;
}

interface DiffRow {
  field: string;
  from: string;
  to: string;
}

/** An SSE event from a fleetd play stream: a step, or the terminating done/error frame. */
type PlayEvent =
  | ({ type?: "step" } & PlayStep)
  | { type: "done"; ok: boolean; managed_id?: string | null }
  | { type: "error"; detail: string };

const SQUADS = ["s0", "s1", "s2", "s3"];
const GPU_ARCHES = ["pascal", "volta", "ampere", "bc250"];
const SERVER_KINDS = ["llamacpp", "vllm"];

/** Run a fleetd play over SSE, re-rendering the widget as each step arrives. Returns final ok. */
async function streamPlay(
  ui: ExtensionUIContext,
  title: string,
  method: "POST",
  path: string,
): Promise<boolean> {
  const steps: PlayStep[] = [];
  let done = false;
  let ok = false;

  const render = (footer: string) => {
    const lines = [title, ...steps.map((s) => `  ${s.ok ? "✓" : "✗"} ${s.name}${s.detail ? ` — ${s.detail}` : ""}`)];
    lines.push(footer);
    ui.setWidget("dnc-deploy", lines);
  };
  render("running…");

  for await (const ev of fleetdStream<PlayEvent>(method, path)) {
    if (ev.type === "done") {
      done = true;
      ok = ev.ok;
    } else if (ev.type === "error") {
      steps.push({ name: "error", ok: false, detail: ev.detail });
    } else {
      steps.push(ev as PlayStep);
      render("running…");
    }
  }
  render(!done ? "stream ended early." : ok ? "done." : "FAILED — see steps above.");
  return done && ok;
}

/** Pick an existing host, or add + preflight a new one. Returns undefined on cancel. */
async function pickHost(ui: ExtensionUIContext): Promise<HostRow | undefined> {
  const hosts = await fleetdGet<HostRow[]>("/hosts");
  const ADD = "+ add a new host…";
  const choice = await ui.select(
    "Deploy target",
    [...hosts.map((h) => `${h.id} (${h.gpu_count}x ${h.gpu_arch ?? "api"}, ${h.squad})`), ADD],
  );
  if (choice === undefined) return undefined;
  if (choice !== ADD) return hosts[hosts.findIndex((h) => choice.startsWith(`${h.id} `))];

  const id = await ui.input("New host id (slug)", "rig-3090-b");
  if (!id) return undefined;
  const address = await ui.input("SSH address", "10.0.0.22");
  if (!address) return undefined;
  const ssh_user = (await ui.input("SSH user", "root")) || "root";
  const squad = await ui.select("Squad", SQUADS);
  if (!squad) return undefined;
  const gpu_arch = await ui.select("GPU arch", GPU_ARCHES);
  if (!gpu_arch) return undefined;
  const gpu_count = Number((await ui.input("GPU count", "1")) || "1");

  const host: HostRow = { id, address, squad, gpu_arch, gpu_count };
  await fleetdSend<HostRow>("PUT", `/hosts/${id}`, { ...host, ssh_user });

  ui.setWidget("dnc-deploy", [`preflighting ${id}…`]);
  const pf = await fleetdSend<Record<string, { ok: boolean; output: string }>>("POST", `/hosts/${id}/preflight`);
  const pfLines = Object.entries(pf).map(([k, v]) => `  ${v.ok ? "✓" : "✗"} ${k}: ${v.output || "(no output)"}`);
  ui.setWidget("dnc-deploy", [`preflight ${id}`, ...pfLines]);
  const allOk = Object.values(pf).every((v) => v.ok);
  if (!allOk && !(await ui.confirm("Preflight had failures", "Continue with this host anyway?"))) {
    return undefined;
  }
  return host;
}

/** Handle an already-discovered adopted server: keep monitor-only, or migrate to standard. */
async function handleAdopted(ui: ExtensionUIContext, host: HostRow, dep: DeploymentRow): Promise<void> {
  const ADOPT = "Adopt as-is (monitor-only, never touched by plays)";
  const MIGRATE = "Migrate to a standard managed Docker deployment";
  const path = await ui.select(`${dep.model_id} on :${dep.port}`, [ADOPT, MIGRATE]);
  if (path === undefined) return;
  if (path === ADOPT) {
    ui.notify(`${dep.id} kept as adopted (monitor-only).`, "info");
    return;
  }

  const portStr = await ui.input("New port for the standard container (side-by-side)", String(dep.port + 1));
  if (!portStr) return;
  const newPort = Number(portStr);
  const targetVersion = (await ui.input("Standard image tag", "latest")) || "latest";

  const q = `new_port=${newPort}&target_version=${encodeURIComponent(targetVersion)}`;
  const plan = await fleetdGet<{ diff: DiffRow[] }>(`/deployments/${dep.id}/migration-plan?${q}`);
  const diffLines = plan.diff.length
    ? plan.diff.map((r) => `  ${r.field}: ${r.from}  →  ${r.to}`)
    : ["  (no differences)"];
  ui.setWidget("dnc-deploy", [`migration plan for ${dep.id}`, ...diffLines]);
  if (!(await ui.confirm("Migrate?", "Deploy the standard container, verify health, then stop the old server."))) {
    return;
  }

  await streamPlay(ui, `migrate ${dep.id} → :${newPort}`, "POST", `/deployments/${dep.id}/migrate/stream?${q}`);
}

/** Fresh deploy of a new model onto a host. */
async function freshDeploy(ui: ExtensionUIContext, host: HostRow): Promise<void> {
  const server = await ui.select("Server", SERVER_KINDS);
  if (!server) return;
  const defaultTag = server === "vllm" ? "latest" : "server-cuda-latest";
  const version = (await ui.input("Image tag", defaultTag)) || defaultTag;
  const modelId = await ui.input(
    "Model id / GGUF repo path",
    server === "vllm" ? "Qwen/Qwen3-8B" : "unsloth/Qwen3-4B-GGUF/Qwen3-4B-Q4_K_M.gguf",
  );
  if (!modelId) return;
  const quant = (await ui.input("Quantization (blank = none)", "")) || null;
  const ctxWindow = Number((await ui.input("Context window", "32768")) || "32768");
  const port = Number((await ui.input("Port", server === "vllm" ? "8000" : "8080")) || "8000");

  const depId = `${host.id}-${modelId.split("/").pop()?.replace(/\.(gguf|safetensors)$/, "").toLowerCase()}`;
  const dep = {
    id: depId,
    host_id: host.id,
    server,
    server_version: version,
    model_id: modelId,
    quant,
    context_window: ctxWindow,
    port,
    management: "docker",
  };

  const summary = [
    `deploy ${depId}`,
    `  host: ${host.id}   server: ${server}:${version}`,
    `  model: ${modelId}${quant ? ` (${quant})` : ""}   ctx: ${ctxWindow}   port: ${port}`,
  ];
  ui.setWidget("dnc-deploy", summary);
  if (!(await ui.confirm("Deploy?", "Pull image, fetch model if absent, (re)create container, health-check."))) {
    return;
  }

  await fleetdSend("PUT", `/deployments/${depId}`, dep);
  await streamPlay(ui, `deploy ${depId}`, "POST", `/deployments/${depId}/apply/stream`);
}

export function registerDeployCommand(pi: ExtensionAPI): void {
  pi.registerCommand("deploy", {
    description: "Wizard: stand up, adopt, or migrate an inference server on a fleet host",
    handler: async (_args, ctx) => {
      const ui = ctx.ui;
      try {
        const host = await pickHost(ui);
        if (!host) {
          ui.notify("deploy: cancelled", "info");
          return;
        }

        ui.setWidget("dnc-deploy", [`scanning ${host.id} for existing servers…`]);
        const found = await fleetdSend<DeploymentRow[]>("POST", `/hosts/${host.id}/discover`);
        const adopted = found.filter((d) => d.management === "adopted");

        if (adopted.length > 0) {
          const NEW = "+ deploy a new model";
          const pick = await ui.select(
            `${host.id}: ${adopted.length} existing server(s) found`,
            [...adopted.map((d) => `existing: ${d.model_id} :${d.port}`), NEW],
          );
          if (pick === undefined) return;
          if (pick !== NEW) {
            const dep = adopted[adopted.findIndex((d) => pick === `existing: ${d.model_id} :${d.port}`)];
            await handleAdopted(ui, host, dep);
            return;
          }
        }
        await freshDeploy(ui, host);
      } catch (err) {
        ui.notify(`deploy failed: ${err}`, "error");
      }
    },
  });
}
