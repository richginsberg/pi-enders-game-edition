#!/usr/bin/env bash
# Bootstrap a divide-and-conquer control-plane box from bare Ubuntu.
#
# Idempotent, NO sudo required for this part — installs uv, a managed Python 3.11
# venv, and the four services (router, fleetd, context, litellm) under ~/dnc, plus
# local config + secret templates. The sudo-only bits (systemd units, Postgres,
# embedding server) are printed at the end and documented in deploy/README.md.
#
# Usage (run ON the box, from a checkout of this repo):
#   ./deploy/bootstrap.sh
# The repo must already be on the box. No git? See deploy/README.md "Getting the repo there".
#
# Why this shape (learned standing up the first box):
#   - Ubuntu's system Python may be 3.14 with no pip and too new for LiteLLM wheels
#     -> use uv's managed 3.11, no sudo.
#   - One shared venv: litellm[proxy] + router + fleetd + context deps are compatible.
set -euo pipefail

DNC_HOME="${DNC_HOME:-$HOME/dnc}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root (deploy/..)
PYTHON_VERSION="${DNC_PYTHON_VERSION:-3.11}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# 1. uv (static binary, ~/.local/bin, no sudo) --------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
say "uv $(uv --version)"

# 2. venv + service installs --------------------------------------------------------
mkdir -p "$DNC_HOME"
cd "$DNC_HOME"
if [ ! -d "$DNC_HOME/.venv" ]; then
  say "creating Python $PYTHON_VERSION venv at $DNC_HOME/.venv"
  uv venv --python "$PYTHON_VERSION"
fi
say "installing litellm + router + fleetd + context into the venv"
uv pip install "litellm[proxy]"
uv pip install -e "$REPO/router" -e "$REPO/fleetd" -e "$REPO/context"

VENV="$DNC_HOME/.venv/bin"
"$VENV/python" -c "import litellm, dnc_router.strategy, fleetd.api, context.service; print('all imports OK')"

# 3. local config from templates (never overwrite existing local edits) -------------
copy_template() { # src dst
  if [ -f "$2" ]; then say "keep existing $2"; else say "seed $2 from template"; cp "$1" "$2"; fi
}
copy_template "$REPO/router/litellm-config.example.yaml" "$DNC_HOME/litellm-config.yaml"
copy_template "$REPO/fleetd/hosts.example.yaml"          "$DNC_HOME/hosts.yaml"

# 4. secrets skeleton (chmod 600) ---------------------------------------------------
if [ ! -f "$DNC_HOME/.env" ]; then
  say "writing secrets skeleton $DNC_HOME/.env (fill these in)"
  umask 077
  cat > "$DNC_HOME/.env" <<'EOF'
# divide-and-conquer control-plane secrets — LOCAL ONLY, chmod 600, never committed.
LITELLM_MASTER_KEY=
GLM_API_KEY=
DNC_S0_MODEL=openai/glm-5.2
DNC_S1_API_BASE=http://REPLACE-WITH-RIG-IP:5000/v1
# context store (set once Postgres+pgvector is up — see deploy/README.md)
DNC_PG_DSN=postgresql:///dnc_context
DNC_EMBED_API_BASE=http://127.0.0.1:8090/v1
DNC_EMBED_MODEL=embed:qwen3
DNC_EMBED_API_KEY=none
EOF
  chmod 600 "$DNC_HOME/.env"
else
  say "keep existing $DNC_HOME/.env"
fi

# 5. launcher for the gateway (used by the systemd unit) ----------------------------
cat > "$DNC_HOME/start-litellm.sh" <<EOF
#!/bin/bash
cd "$DNC_HOME"
set -a; . "$DNC_HOME/.env"; set +a
exec "$VENV/litellm" --config "$DNC_HOME/litellm-config.yaml" --host 0.0.0.0 --port 4000
EOF
chmod +x "$DNC_HOME/start-litellm.sh"

# 6. render systemd unit templates with THIS box's user/home ------------------------
# The committed units use __DNC_HOME__/__DNC_USER__ placeholders (no local paths in
# the public repo); render real ones here for install.
mkdir -p "$DNC_HOME/systemd"
for unit in "$REPO"/deploy/dnc-*.service; do
  sed -e "s|__DNC_HOME__|$DNC_HOME|g" -e "s|__DNC_USER__|$USER|g" \
      "$unit" > "$DNC_HOME/systemd/$(basename "$unit")"
done
say "rendered systemd units into $DNC_HOME/systemd/"

say "DONE (no-sudo phase). Layout under $DNC_HOME:"
ls -1 "$DNC_HOME"
cat <<EOF

Next steps (need sudo / decisions — see deploy/README.md):
  1. Fill in $DNC_HOME/.env (LITELLM_MASTER_KEY, GLM_API_KEY, DNC_S1_API_BASE).
  2. Install services (rendered with real paths — NOT the placeholder templates):
       sudo cp $DNC_HOME/systemd/dnc-*.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now dnc-litellm dnc-fleetd dnc-context
  3. (optional) Postgres+pgvector for the context store — see README §Postgres.
  4. (optional) Embedding server:  $REPO/deploy/install-embeddings.sh
  5. Verify:  curl -s localhost:4000/health/readiness ; curl -s localhost:7431/healthz
EOF
