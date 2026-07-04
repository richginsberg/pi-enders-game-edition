#!/usr/bin/env bash
# Install a self-hosted Qwen3-Embedding server (llama.cpp CPU) on the control-plane box.
# NO sudo required. Bakes in the gotchas found benchmarking the first box:
#   - llama.cpp release assets are .tar.gz (llama-b<N>-bin-ubuntu-x64.tar.gz), not .zip
#   - the CPU binary needs libgomp.so.1; get it without sudo via `apt-get download` + dpkg-deb -x
#   - serves the OpenAI /v1/embeddings that LiteLLM's `embed:qwen3` points at
# Benchmarked on an i7-9700T: query p50 ~39ms, chunk ~44ms (~7266 tok/s) at -t 4.
set -euo pipefail

EMB_HOME="${DNC_EMB_HOME:-$HOME/dnc/embeddings}"
MODEL_REPO="Qwen/Qwen3-Embedding-0.6B-GGUF"
MODEL_FILE="Qwen3-Embedding-0.6B-Q8_0.gguf"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

mkdir -p "$EMB_HOME"/{bin,libs,models}
cd "$EMB_HOME"

# 1. llama.cpp CPU build (latest release) -------------------------------------------
if [ ! -x "$(find bin -name llama-server -type f 2>/dev/null | head -1)" ]; then
  say "fetching latest llama.cpp ubuntu-x64 CPU build"
  URL=$(curl -s https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
        | grep -oE 'https://[^"]*llama-b[0-9]+-bin-ubuntu-x64\.tar\.gz' | head -1)
  [ -n "$URL" ] || { echo "could not find release asset"; exit 1; }
  curl -sL -o llama.tar.gz "$URL"
  tar xzf llama.tar.gz -C bin --strip-components=0
  rm -f llama.tar.gz
fi
SRV="$(find "$EMB_HOME/bin" -name llama-server -type f | head -1)"
LLAMA_LIBDIR="$(dirname "$SRV")"
chmod +x "$SRV"

# 2. libgomp.so.1 without sudo ------------------------------------------------------
if [ -z "$(find libs -name 'libgomp.so*' 2>/dev/null | head -1)" ]; then
  say "fetching libgomp1 (no sudo) via apt-get download"
  ( cd libs && apt-get download libgomp1 && dpkg-deb -x libgomp1*.deb . && rm -f libgomp1*.deb )
fi
GOMP_DIR="$(dirname "$(find "$EMB_HOME/libs" -name 'libgomp.so.1' | head -1)")"

# 3. model --------------------------------------------------------------------------
if [ ! -f "models/$MODEL_FILE" ]; then
  say "downloading $MODEL_FILE (~639MB)"
  curl -sL -o "models/$MODEL_FILE" "https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_FILE"
fi

cat > "$EMB_HOME/start-embeddings.sh" <<EOF
#!/bin/bash
export LD_LIBRARY_PATH="$GOMP_DIR:$LLAMA_LIBDIR"
exec "$SRV" -m "$EMB_HOME/models/$MODEL_FILE" --embeddings -t 4 --host 127.0.0.1 --port 8090
EOF
chmod +x "$EMB_HOME/start-embeddings.sh"

say "installed. Test:  $EMB_HOME/start-embeddings.sh   (then curl :8090/v1/embeddings)"
say "For durability, install deploy/dnc-embeddings.service (edit paths to match $EMB_HOME)."
echo "LD_LIBRARY_PATH for the unit: $GOMP_DIR:$LLAMA_LIBDIR"
