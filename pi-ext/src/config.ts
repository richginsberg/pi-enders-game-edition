/** Shared endpoints for the extension. Override via env. */
export const LITELLM_BASE_URL = process.env.DNC_LITELLM_URL ?? "http://localhost:4000";
export const LITELLM_API_KEY = "$LITELLM_MASTER_KEY";
export const FLEETD_BASE_URL = process.env.DNC_FLEETD_URL ?? "http://localhost:7431";

export async function fleetdGet<T>(path: string): Promise<T> {
  const res = await fetch(`${FLEETD_BASE_URL}${path}`);
  if (!res.ok) throw new Error(`fleetd ${path}: ${res.status}`);
  return (await res.json()) as T;
}
