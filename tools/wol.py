#!/usr/bin/env python3
"""Send a Wake-on-LAN magic packet — power a fleet node on from the control plane.

Stdlib only. The sender must be on the target's L2 broadcast domain (magic packets
are broadcast, not routed). The target's NIC must be WoL-armed (`ethtool <if>` shows
`Wake-on: g`; persist via NetworkManager: `nmcli con mod <con> 802-3-ethernet.wake-on-lan magic`)
and its BIOS must have Wake-on-LAN enabled with ErP/Deep-Sleep disabled.

Verified on the BC-250 fleet (r8169 NIC) — the wake half of scale-to-zero.

Usage:
  python3 tools/wol.py <MAC> [broadcast-addr]
  python3 tools/wol.py a8:a1:59:b3:68:cd 192.168.1.255
"""
from __future__ import annotations

import socket
import sys


def magic_packet(mac: str) -> bytes:
    hexmac = mac.replace(":", "").replace("-", "").strip()
    if len(hexmac) != 12:
        raise ValueError(f"bad MAC: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexmac) * 16


def wake(mac: str, broadcast: str = "255.255.255.255", ports: tuple[int, ...] = (9, 7)) -> None:
    pkt = magic_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in ports:
        s.sendto(pkt, (broadcast, port))
    s.close()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mac = sys.argv[1]
    broadcast = sys.argv[2] if len(sys.argv) > 2 else "255.255.255.255"
    wake(mac, broadcast)
    print(f"magic packet sent to {mac} via {broadcast}:9,7")


if __name__ == "__main__":
    main()
