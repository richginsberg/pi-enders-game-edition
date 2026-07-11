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
