set -e
log(){ echo "[prov-ubuntu] $*"; }
# 1. podman
if command -v podman >/dev/null; then log "podman present"; else
  log "installing podman (apt)"; sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq podman >/dev/null; fi
podman --version
# 1b. serving image from Docker Hub (patched-Mesa llama.cpp; public repo, no node streaming).
DNC_IMAGE="${DNC_IMAGE:-docker.io/machinez/llamacpp-bc250:latest}"
if podman image exists "$DNC_IMAGE" 2>/dev/null; then log "image present ($DNC_IMAGE)"; else
  log "pulling serving image $DNC_IMAGE"; podman pull "$DNC_IMAGE"; fi
# 2. GTT: live ttm write (this boot) + persist gttsize+pages_limit via GRUB (reboot activates amdgpu GTT domain)
GTT_MB=14336; TTM_PAGES=$((GTT_MB*256))
cur=$(cat /sys/module/ttm/parameters/pages_limit 2>/dev/null || echo 0)
if [ "${cur:-0}" -lt "$TTM_PAGES" ]; then echo "$TTM_PAGES" | sudo tee /sys/module/ttm/parameters/pages_limit >/dev/null; log "live ttm.pages_limit -> $(cat /sys/module/ttm/parameters/pages_limit)"; fi
if grep -q "ttm.pages_limit=$TTM_PAGES" /proc/cmdline && grep -q "amdgpu.gttsize=$GTT_MB" /proc/cmdline; then
  log "GTT cmdline already set"
else
  ARGS="amdgpu.gttsize=$GTT_MB ttm.pages_limit=$TTM_PAGES amdgpu.sg_display=0"
  if grep -q "GRUB_CMDLINE_LINUX_DEFAULT" /etc/default/grub; then
    sudo sed -i "s#^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"#GRUB_CMDLINE_LINUX_DEFAULT=\"\1 $ARGS\"#" /etc/default/grub
  else
    echo "GRUB_CMDLINE_LINUX_DEFAULT=\"$ARGS\"" | sudo tee -a /etc/default/grub >/dev/null
  fi
  # dedupe accidental doubles
  sudo sed -i "s/\( amdgpu.gttsize=$GTT_MB\)\{2,\}/ amdgpu.gttsize=$GTT_MB/; s/\( ttm.pages_limit=$TTM_PAGES\)\{2,\}/ ttm.pages_limit=$TTM_PAGES/" /etc/default/grub
  sudo update-grub >/dev/null 2>&1 || sudo update-grub
  log "GRUB updated with: $ARGS (REBOOT needed to size amdgpu GTT domain)"
fi
# 2b. SMU power governor (cyan-skillfish-governor-smu). GUARD: the governor needs the SMU
# gpu_metrics table to read load and drive clocks. On kernels that regressed BC-250 SMU
# support (observed: Ubuntu 26.04 / kernel 7.0 — empty gpu_metrics, DPM force-level not
# writable) the governor CAN'T ramp and actively pins the GPU LOW (~1000MHz -> ~5x slower
# inference than kernel 6.14). Skip it there; native DPM (~1500MHz) is the better of bad
# options. Only install the governor if the SMU metrics table is actually populated.
GOV_REPO="https://github.com/filippor/cyan-skillfish-governor.git"
GPU_DEV=$(readlink -f /sys/class/drm/renderD128/device 2>/dev/null)
METRICS_BYTES=$(sudo cat "$GPU_DEV/gpu_metrics" 2>/dev/null | wc -c)
if [ "${METRICS_BYTES:-0}" -lt 32 ]; then
  log "WARNING: SMU gpu_metrics empty ($METRICS_BYTES bytes) — this kernel's BC-250 power mgmt is broken."
  log "         Skipping SMU governor (it would pin the GPU low). Inference will be SLOW until the"
  log "         kernel has working Cyan Skillfish SMU support (kernel 6.14 works; 7.0 regressed)."
elif systemctl is-active --quiet cyan-skillfish-governor-smu; then log "SMU governor active"; else
  sudo systemctl disable --now oberon-governor 2>/dev/null || true
  if ! systemctl list-unit-files 2>/dev/null | grep -q cyan-skillfish-governor-smu; then
    log "installing governor build deps"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl build-essential pkg-config libdrm-dev >/dev/null
    sysver=$(rustc --version 2>/dev/null | grep -oP '1\.\K[0-9]+' || echo 0)
    if command -v cargo >/dev/null && [ "${sysver:-0}" -ge 87 ]; then log "system rust $(rustc --version)"
    elif [ -x "$HOME/.cargo/bin/cargo" ]; then source "$HOME/.cargo/env"
    else log "installing rustup stable"; curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable; source "$HOME/.cargo/env"; fi
    [ -d ~/cyan-skillfish-governor ] || git clone --branch smu --depth 1 "$GOV_REPO" ~/cyan-skillfish-governor
    ( cd ~/cyan-skillfish-governor && log "building governor (cargo --release)" && cargo build --release \
        && cp target/release/cyan-skillfish-governor-smu ./cyan-skillfish-governor-smu && sudo bash scripts/install.sh )
  fi
  log "writing inference-tuned governor config"
  sudo mkdir -p /etc/cyan-skillfish-governor-smu
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

# 3. linger
sudo loginctl enable-linger "$USER"; log "linger enabled"
# 4. WoL: arm now + persist via systemd oneshot (netplan/networkd)
NIC=enp4s0
sudo ethtool -s "$NIC" wol g 2>/dev/null || true
sudo tee /etc/systemd/system/wol-$NIC.service >/dev/null <<UNIT
[Unit]
Description=Arm Wake-on-LAN on $NIC
After=network.target
[Service]
Type=oneshot
ExecStart=/usr/sbin/ethtool -s $NIC wol g
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now wol-$NIC.service >/dev/null 2>&1 || true
log "WoL armed+persisted on $NIC (magic): $(sudo ethtool $NIC 2>/dev/null | grep -i 'Wake-on' | tail -1)"
echo "[prov-ubuntu] host provisioning done — REBOOT required for GTT domain"
