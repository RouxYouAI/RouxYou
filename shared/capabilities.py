"""
Capability registry management.
Tracks what the system can and cannot do.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


class CapabilityRegistry:

    def __init__(self, config_path: str = "../config/capabilities.json"):
        self.config_path = Path(config_path)
        self.capabilities = self._load_capabilities()

    def _load_capabilities(self) -> Dict:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Capability registry not found: {self.config_path}")
        with open(self.config_path, 'r') as f:
            return json.load(f)

    def _save_capabilities(self):
        self.capabilities['last_updated'] = datetime.now().isoformat()
        with open(self.config_path, 'w') as f:
            json.dump(self.capabilities, f, indent=2)

    def has_capability(self, capability_name: str) -> bool:
        cap = self.capabilities['capabilities'].get(capability_name)
        return cap is not None and cap.get('enabled', False)

    def get_capability(self, capability_name: str) -> Optional[Dict]:
        return self.capabilities['capabilities'].get(capability_name)

    def list_capabilities(self, enabled_only: bool = False) -> List[str]:
        caps = self.capabilities['capabilities']
        if enabled_only:
            return [name for name, details in caps.items() if details.get('enabled', False)]
        return list(caps.keys())

    def enable_capability(self, capability_name: str, worker: str, methods: List[str]):
        if capability_name not in self.capabilities['capabilities']:
            self.capabilities['capabilities'][capability_name] = {
                "enabled": True,
                "description": f"Dynamically added: {capability_name}",
                "worker": worker,
                "methods": methods
            }
        else:
            self.capabilities['capabilities'][capability_name]['enabled'] = True
            self.capabilities['capabilities'][capability_name]['worker'] = worker
            self.capabilities['capabilities'][capability_name]['methods'] = methods
        self._save_capabilities()

    def disable_capability(self, capability_name: str):
        if capability_name in self.capabilities['capabilities']:
            self.capabilities['capabilities'][capability_name]['enabled'] = False
            self._save_capabilities()

    def get_capabilities_summary(self) -> str:
        caps = self.capabilities['capabilities']
        enabled = [name for name, details in caps.items() if details.get('enabled', False)]
        disabled = [name for name, details in caps.items() if not details.get('enabled', False)]
        summary = "Current Capabilities:\n"
        summary += f"  Enabled ({len(enabled)}): {', '.join(enabled)}\n"
        summary += f"  Disabled ({len(disabled)}): {', '.join(disabled)}\n"
        return summary


def get_registry() -> CapabilityRegistry:
    return CapabilityRegistry()
