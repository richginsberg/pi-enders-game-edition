#!/usr/bin/env bash
# Idempotent BC-250 S3-node OS provisioning. Runs ON the node (fleetd pushes+runs it via SSH).
#
# Covers the artifact-free, node-local setup:
#   1. podman (rootless container runtime)
#   2. SMU GPU governor (build from source via rustup, install, inference-tuned config)
#   3. Wake-on-LAN: arm + persist on the onboard NIC (the scale-to-zero wake path)
#   4. linger (so the user systemd manager — and thus the llama.cpp unit — runs at boot)
#
# NOT here (need control-plane artifacts; the fleetd provision play does these):
#   image load, model transfer, the container systemd --user unit, LiteLLM registration.
#
# Prereqs (manual / physical, per bc250-power-tuning + bc250-vulkan notes):
#   - BIOS: 512 MB dynamic VRAM split (→ ~14 GB host RAM, required for 262k context)
#   - patched Mesa already baked into the dnc/llamacpp-bc250 image
#
# Idempotent: every step checks state before acting; safe to re-run.
set -euo pipefail

GOV_REPO="https://github.com/filippor/cyan-skillfish-governor.git"
# Onboard NIC for WoL (NOT a USB NIC — USB loses power in S5 and can't wake). Auto-detect
# the default-route iface unless overridden as $1.
NIC="${1:-$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' | head -1)}"

log() { echo "[provision] $*"; }
NEED_REBOOT=0

# 0. Preflight: disable rogue pre-existing inference services -------------------------
# A reused node may carry an old auto-start (e.g. a hand-rolled llama.service from a
# prior experiment) that fights our container for port 8080 / the GPU / memory and
# manifests as mysterious contention (OOM thrash, "won't load"). Disable any system
# unit whose ExecStart runs llama-server.
for f in $(sudo grep -rilE "llama-server|llama\.cpp" /etc/systemd/system/*.service 2>/dev/null); do
  svc=$(basename "$f")
  log "disabling rogue pre-existing service: $svc"
  sudo systemctl disable --now "$svc" 2>/dev/null || true
done

# 1. podman ---------------------------------------------------------------------------
if command -v podman >/dev/null; then log "podman present"; else
  # skip weak deps: plain `dnf install podman` drags in ~228 MB of qemu-user-static
  # multi-arch emulators we never use on an x86 inference node.
  log "installing podman"; sudo dnf -y --setopt=install_weak_deps=False install podman
fi

# 2. SMU governor ---------------------------------------------------------------------
if systemctl is-active --quiet cyan-skillfish-governor-smu; then
  log "SMU governor already active"
else
  log "disabling oberon governor (if present)"
  sudo systemctl disable --now oberon-governor 2>/dev/null || true
  if ! systemctl list-unit-files 2>/dev/null | grep -q cyan-skillfish-governor-smu; then
    log "building SMU governor from source"
    # The governor's edition-2024 / zbus deps need rustc >= 1.87. Prefer the system
    # toolchain if it's new enough (varies per node's dnf state); else an existing
    # rustup; else install rustup. (Fedora 40 shipped 1.86 which is too old.)
    sysver=$(rustc --version 2>/dev/null | grep -oP '1\.\K[0-9]+' || echo 0)
    if command -v cargo >/dev/null && [ "${sysver:-0}" -ge 87 ]; then
      log "using system rust $(rustc --version)"
    elif [ -x "$HOME/.cargo/bin/cargo" ]; then
      log "using existing rustup toolchain"; source "$HOME/.cargo/env"
    else
      log "system rust too old ($sysver); installing rustup stable"
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
      source "$HOME/.cargo/env"
    fi
    [ -d ~/cyan-skillfish-governor ] || git clone --branch smu --depth 1 "$GOV_REPO" ~/cyan-skillfish-governor
    cd ~/cyan-skillfish-governor
    cargo build --release
    cp target/release/cyan-skillfish-governor-smu ./cyan-skillfish-governor-smu  # install.sh expects it here
    sudo bash scripts/install.sh
  fi
  log "writing inference-tuned governor config"
  # min 400 MHz idle (the sub-1000 lever), 2000 MHz load; conservative undervolt; load-target
  # tuned so any GPU activity holds clock during a request (see bc250-power-tuning memory).
  sudo tee /etc/cyan-skillfish-governor-smu/config.toml >/dev/null <<'TOML'
[gpu-usage]
fix-metrics = true
method = "busy-flag"
flush-every = 10
[gpu]
set-method = "smu"
[dbus]
enabled = false
[frequency-range]
min = 400
max = 2000
[timing]
[timing.intervals]
sample = 2000
adjust = 20000
burst-samples = 0
down-events = 10
[timing.ramp-rates]
normal = 8.0
burst = 200.0
[frequency-thresholds]
adjust = 10
[load-target]
upper = 0.35
lower = 0.15
[temperature]
throttling = 85
throttling_recovery = 80
[[safe-points]]
frequency = 400
voltage = 750
[[safe-points]]
frequency = 1000
voltage = 820
[[safe-points]]
frequency = 2000
voltage = 950
TOML
  sudo systemctl enable --now cyan-skillfish-governor-smu
fi

# 3. Wake-on-LAN (onboard NIC) --------------------------------------------------------
if [ -z "$NIC" ] || [ ! -e "/sys/class/net/$NIC" ]; then
  log "WARNING: NIC '$NIC' not found; skipping WoL setup"
else
  log "arming WoL on $NIC (MAC $(cat /sys/class/net/$NIC/address))"
  sudo ethtool -s "$NIC" wol g 2>/dev/null || log "WARNING: ethtool wol set failed"
  CON=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: -v d="$NIC" '$2==d{print $1; exit}')
  if [ -n "$CON" ]; then
    sudo nmcli con mod "$CON" 802-3-ethernet.wake-on-lan magic && log "WoL persisted via NetworkManager ($CON)"
  else
    log "WARNING: no active NM connection for $NIC; WoL armed at runtime but not persisted"
  fi
fi

# 4. linger ---------------------------------------------------------------------------
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  log "linger already enabled"
else
  log "enabling linger for $USER"; sudo loginctl enable-linger "$USER"
fi

# 5. GTT size (CRITICAL for the dynamic-VRAM split) -----------------------------------
# With 512 MB VRAM, the model lives in GTT (GART aperture into system RAM). amdgpu derives
# GTT size from BIOS memory config — some BIOSes give only ~1/2 RAM (e.g. 7.6 GB), too
# small for a 9 GB+ model → `radv: Not enough memory for command submission` / DeviceLost
# crash. Force a large GTT on the kernel cmdline. Requires a reboot to take effect.
GTT_MB=14336
if grep -q "amdgpu.gttsize" /proc/cmdline; then
  log "amdgpu.gttsize already on cmdline (GTT total: $(cat /sys/class/drm/card*/device/mem_info_gtt_total 2>/dev/null | head -1 | awk '{printf "%.0f MB", $1/1e6}'))"
else
  log "setting amdgpu.gttsize=$GTT_MB via grubby (REBOOT REQUIRED)"
  sudo grubby --update-kernel=ALL --args="amdgpu.gttsize=$GTT_MB"
  NEED_REBOOT=1
fi

echo "[provision] OS provisioning complete. WoL MAC: $(cat /sys/class/net/${NIC:-none}/address 2>/dev/null || echo unknown)"
if [ "$NEED_REBOOT" = "1" ]; then
  echo "[provision] *** REBOOT REQUIRED *** (amdgpu.gttsize) — after reboot verify:"
  echo "[provision]   cat /sys/class/drm/card*/device/mem_info_gtt_total  # must exceed model+KV (~12 GB for 262k)"
fi
