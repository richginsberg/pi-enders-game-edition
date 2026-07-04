#!/bin/sh
# Launch llama-server for the BC-250. All knobs are env-driven (see Dockerfile ENV).
#
# --flash-attn is REQUIRED: q8_0 V-cache errors without it ("V cache quantization
# requires flash_attn"). KV q8_0 at 65536 ctx + q4 weights ≈ 12.1 GiB, right at the
# 12 GiB VRAM carveout — compute buffers spill to GTT (host RAM), so keep host free.
set -eu

: "${DNC_MODEL:?set DNC_MODEL to the mounted .gguf path}"

# Optional custom chat template (the Qwen3-Coder tool-use template lives outside the
# image; mount it and set DNC_CHAT_TEMPLATE). Falls back to the model's embedded one.
template_arg=""
if [ -n "${DNC_CHAT_TEMPLATE:-}" ]; then
    template_arg="--chat-template-file ${DNC_CHAT_TEMPLATE}"
fi

exec llama-server \
    -m "${DNC_MODEL}" \
    -ngl "${DNC_NGL:-99}" \
    -c "${DNC_CTX:-65536}" \
    --cache-type-k "${DNC_KV:-q8_0}" \
    --cache-type-v "${DNC_KV:-q8_0}" \
    --flash-attn \
    --temp "${DNC_TEMP:-0.6}" \
    --host 0.0.0.0 \
    --port "${DNC_PORT:-8080}" \
    --alias "${DNC_ALIAS:-Qwen-Qwen3-Coder-30B-A3B-Instruct}" \
    --jinja ${template_arg}
