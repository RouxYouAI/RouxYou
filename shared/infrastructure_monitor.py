"""
Infrastructure Monitor — local resource and network scanning.

Provides the Proposer with system resource metrics (CPU, RAM, disk)
and basic LAN device discovery via ARP.

Personal IP-based device detection (hypervisors, NAS boxes, specific servers) is
intentionally not included. Add your own detection logic in
identify_opportunities() if needed.
"""

import platform
import socket
import subprocess
import json
import re
import os
import sys
from typing import Dict, List, Any
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.schemas import TaskType

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False


class InfrastructureMonitor:

    def __init__(self):
        self.os_info  = f"{platform.system()} {platform.release()}"
        self.hostname = socket.gethostname()

    def get_local_resources(self) -> Dict[str, Any]:
        """Scan the machine the agent is running on."""
        resources = {
            "hostname":   self.hostname,
            "os":         self.os_info,
            "cpu_count":  os.cpu_count(),
            "ram_total_gb":     0,
            "ram_available_gb": 0,
            "disk_free_gb":     0,
        }
        if _PSUTIL:
            try:
                cpu_usage = psutil.cpu_percent(interval=0.1)
                ram  = psutil.virtual_memory()
                disk = psutil.disk_usage(".")
                resources.update({
                    "cpu_usage_percent":  cpu_usage,
                    "ram_total_gb":       round(ram.total     / (1024 ** 3), 2),
                    "ram_available_gb":   round(ram.available / (1024 ** 3), 2),
                    "disk_free_gb":       round(disk.free     / (1024 ** 3), 2),
                })
            except Exception as e:
                print(f"INFRA: Error reading system stats: {e}")
        return resources

    def scan_network(self) -> List[Dict[str, Any]]:
        """Use ARP to discover LAN neighbors."""
        devices = []
        try:
            result = subprocess.run(
                ["arp", "-a"],
                capture_output=True, text=True, timeout=5,
            )
            entries = re.findall(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+([0-9a-fA-F-]{17})",
                result.stdout,
            )
            for ip, mac in entries:
                if not any(ip.startswith(p) for p in ["224.", "239.", "255.", "169.254."]):
                    devices.append({
                        "ip":  ip,
                        "mac": mac.replace("-", ":").upper(),
                        "hostname": "?",
                    })
        except subprocess.TimeoutExpired:
            print("INFRA: ARP command timed out")
        except Exception as e:
            print(f"INFRA: Network scan error: {e}")
        return devices

    def identify_opportunities(
        self, local: Dict, network: List
    ) -> List[Dict[str, Any]]:
        """
        Analyse resource data and return a list of task-shaped opportunities.

        Add your own device-specific detection here.
        Example:
            for dev in network:
                if dev["ip"] == "192.168.1.x":   # your hypervisor, NAS, etc.
                    opportunities.append({...})
        """
        opportunities = []

        # High available RAM → could run a local vector DB or larger model
        if local.get("ram_available_gb", 0) > 8.0:
            opportunities.append({
                "title": "Deploy Local Vector Database (ChromaDB)",
                "description": (
                    f"Detected {local['ram_available_gb']}GB RAM available. "
                    "A persistent memory server could run locally."
                ),
                "priority": 4,
                "type": TaskType.RESEARCH,
            })

        # Raspberry Pi detection (OUI prefixes)
        for dev in network:
            mac = dev["mac"]
            if any(mac.startswith(prefix) for prefix in
                   ["B8:27:EB", "DC:A6:32", "D8:3A:DD", "E4:5F:01"]):
                opportunities.append({
                    "title": f"Investigate Raspberry Pi at {dev['ip']}",
                    "description": (
                        f"Found a Raspberry Pi ({mac}) at {dev['ip']}. "
                        "Check for running services (OctoPrint, Klipper, etc.)."
                    ),
                    "priority": 5,
                    "type": TaskType.RESEARCH,
                })

        # ── ADD YOUR OWN DEVICE DETECTION HERE ──────────────────────────────
        # for dev in network:
        #     if dev["ip"] == "192.168.1.x":
        #         opportunities.append({
        #             "title": "Integrate my-device",
        #             "description": "...",
        #             "priority": 6,
        #             "type": TaskType.USER_GOAL,
        #         })
        # ─────────────────────────────────────────────────────────────────────

        return opportunities


if __name__ == "__main__":
    monitor = InfrastructureMonitor()
    print("\n🖥️  LOCAL RESOURCES:")
    print(json.dumps(monitor.get_local_resources(), indent=2))
    print("\n🌐 NETWORK DEVICES:")
    devices = monitor.scan_network()
    print(json.dumps(devices, indent=2))
    print("\n💡 OPPORTUNITIES:")
    opps = monitor.identify_opportunities(monitor.get_local_resources(), devices)
    for op in opps:
        op["type"] = op["type"].value
    print(json.dumps(opps, indent=2))
