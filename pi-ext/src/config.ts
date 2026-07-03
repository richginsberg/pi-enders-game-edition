/** Shared endpoints for the extension. Override via env. */
export const LITELLM_BASE_URL = process.env.DNC_LITELLM_URL ?? "http://localhost:4000";
export const LITELLM_API_KEY = "$LITELLM_MASTER_KEY";
export const FLEETD_BASE_URL = process.env.DNC_FLEETD_URL ?? "http://localhost:7431";

export async function fleetdGet<T>(path: string): Promise<T> {
  const res = await fetch(`${FLEETD_BASE_URL}${path}`);
  if (!res.ok) throw new Error(`fleetd ${path}: ${res.status}`);
  return (await res.json()) as T;
}

export async function fleetdSend<T>(method: "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${FLEETD_BASE_URL}${path}`, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`fleetd ${method} ${path}: ${res.status} ${await res.text().catch(() => "")}`);
  return (await res.json()) as T;
}

/** Consume a fleetd Server-Sent Events endpoint, yielding one parsed JSON event per `data:` frame. */
export async function* fleetdStream<T>(method: "POST" | "PUT", path: string, body?: unknown): AsyncGenerator<T> {
  const res = await fetch(`${FLEETD_BASE_URL}${path}`, {
    method,
    headers: { accept: "text/event-stream", ...(body === undefined ? {} : { "content-type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`fleetd ${method} ${path}: ${res.status}`);

  const reader = (res.body as ReadableStream<Uint8Array>).getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (dataLine) yield JSON.parse(dataLine.slice(5).trim()) as T;
    }
  }
}
