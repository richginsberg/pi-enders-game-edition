/**
 * Fleet provider: registers LiteLLM's models (including tier:* virtual models)
 * with Pi at startup. Async factory pattern from extensions.md — models are
 * available to `pi --list-models` and the /model picker.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { LITELLM_API_KEY, LITELLM_BASE_URL } from "./config.js";

interface LiteLlmModel {
  id: string;
  max_input_tokens?: number;
  max_output_tokens?: number;
}

export async function registerFleetProvider(pi: ExtensionAPI): Promise<void> {
  let models: LiteLlmModel[] = [];
  try {
    const res = await fetch(`${LITELLM_BASE_URL}/v1/models`, {
      headers: { Authorization: `Bearer ${process.env.LITELLM_MASTER_KEY ?? ""}` },
    });
    models = ((await res.json()) as { data: LiteLlmModel[] }).data;
  } catch {
    // LiteLLM down: register nothing; /fleet will surface the outage.
    return;
  }

  pi.registerProvider("fleet", {
    name: "DnC Fleet",
    baseUrl: `${LITELLM_BASE_URL}/v1`,
    apiKey: LITELLM_API_KEY,
    api: "openai-completions",
    models: models.map((m) => ({
      id: m.id,
      name: m.id,
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: m.max_input_tokens ?? 32768,
      maxTokens: m.max_output_tokens ?? 8192,
    })),
  });
}
