"""
RouxYou — Permission Tiers & Security Policy (Phase 36)
========================================================
Unified permission model with three tiers + granular file-level overrides.
Centralizes all security configs that were scattered across worker.py and orchestrator.py.

Usage:
    from shared.permissions import policy, PermissionTier

    result = policy.check("write_file", target="worker.py")
    # result.allowed = False
    # result.reason = "System file requires deploy_patch"
    # result.required_tier = PermissionTier.ELEVATED

    result = policy.check("read_file", target="config.yaml")
    # result.allowed = True

    result = policy.check("read_file", target=".env")
    # result.allowed = False
    # result.reason = "Sensitive file blocked"
"""

import re
import logging
from enum import IntEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("roux.permissions")


# ============================================================
# Permission Tiers
# ============================================================

class PermissionTier(IntEnum):
    """Three-tier permission model. Higher = more dangerous."""
    READ_ONLY = 1       # read_file, web_search, rag_query
    WORKSPACE_WRITE = 2 # write_file, patch_file, run_command (safe)
    ELEVATED = 3        # deploy_patch, restart_service, system commands


# Which tier each action requires
ACTION_TIERS = {
    "read_file":        PermissionTier.READ_ONLY,
    "web_search":       PermissionTier.READ_ONLY,
    "rag_query":        PermissionTier.READ_ONLY,
    "verify_fix":       PermissionTier.READ_ONLY,
    "write_file":       PermissionTier.WORKSPACE_WRITE,
    "patch_file":       PermissionTier.WORKSPACE_WRITE,
    "run_command":      PermissionTier.WORKSPACE_WRITE,
    "deploy_patch":     PermissionTier.ELEVATED,
    "restart_service":  PermissionTier.ELEVATED,
}


# ============================================================
# File Security Classifications
# ============================================================

# Files that can NEVER be modified by any action (immutable supervisor layer)
IMMUTABLE_FILES = frozenset({"watchtower.py", "lifecycle.py"})

# Files that require deploy_patch (blue-green pipeline) instead of direct write/patch
DEPLOY_ONLY_FILES = frozenset({
    "worker.py", "orchestrator.py", "coder.py", "gateway.py",
    "dashboard.py", "companion.py", "memory_agent.py",
    "deployer.py", "schemas.py", "logger.py", "activity.py",
    "task_registry.py", "codebase_index.py", "infrastructure_monitor.py",
})

# Files blocked from reading (credentials)
SENSITIVE_FILES = frozenset({".env", ".env.local", ".env.production", ".env.secret"})

# Core system files with extra write protection (min-size check)
SYSTEM_FILES = frozenset({
    "orchestrator.py", "coder.py", "worker.py", "memory.py",
    "schemas.py", "companion.py", "dashboard.py", "watchtower.py",
})

# Maps filenames to their service (for deploy routing)
FILE_TO_SERVICE = {
    "worker.py": "worker",
    "orchestrator.py": "orchestrator",
    "coder.py": "coder",
    "companion.py": "orchestrator",
    "registry.py": "coder",
    "deployer.py": "worker",
    "schemas.py": "worker",
    "logger.py": "worker",
    "activity.py": "worker",
    "task_registry.py": "orchestrator",
    "codebase_index.py": "coder",
    "infrastructure_monitor.py": "orchestrator",
    "dashboard.py": "orchestrator",
    "gateway.py": "gateway",
    "memory_agent.py": "worker",
}

# Path whitelist — operations only allowed within these directories
SAFE_PATHS = [
    Path(__file__).parent.parent.resolve(),  # Project root (auto-detected)
]


# ============================================================
# Credential Redaction
# ============================================================

CREDENTIAL_PATTERNS = [
    re.compile(r'(?i)(TOKEN|PASSWORD|SECRET|API_KEY|APIKEY|AUTH|CREDENTIAL|PRIVATE_KEY)\s*[=:]\s*\S+'),
    re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*'),
    re.compile(r'(?<=[=:\s])[A-Za-z0-9+/\-._]{40,}={0,3}'),
]


def redact_credentials(text: str) -> str:
    """Redact credential-like patterns from text."""
    for p in CREDENTIAL_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


# ============================================================
# Permission Check Result
# ============================================================

@dataclass
class PermissionResult:
    """Result of a permission check."""
    allowed: bool
    reason: str = ""
    required_tier: PermissionTier = PermissionTier.READ_ONLY
    redirect_action: str = ""  # e.g., "deploy_patch" when write_file is blocked
    redirect_service: str = ""  # e.g., "worker" for deploy routing

    @staticmethod
    def allow() -> "PermissionResult":
        return PermissionResult(allowed=True)

    @staticmethod
    def deny(reason: str, tier: PermissionTier = PermissionTier.READ_ONLY,
             redirect_action: str = "", redirect_service: str = "") -> "PermissionResult":
        return PermissionResult(
            allowed=False, reason=reason, required_tier=tier,
            redirect_action=redirect_action, redirect_service=redirect_service
        )


# ============================================================
# Permission Policy (Singleton)
# ============================================================

class PermissionPolicy:
    """
    Centralized permission checking. Replaces scattered checks in worker.py.

    Usage:
        result = policy.check("write_file", target="worker.py")
        if not result.allowed:
            if result.redirect_action:
                # Route to deploy_patch instead
            else:
                # Block entirely
    """

    def check(self, action: str, target: str = "", content: str = "") -> PermissionResult:
        """
        Check if an action is permitted on a target.

        Args:
            action: The action type (read_file, write_file, etc.)
            target: File path or target identifier
            content: Optional content being written (for size checks)
        """
        filename = Path(target).name.lower() if target else ""
        original_filename = Path(target).name if target else ""

        # 1. Immutable files — absolute block
        if action in ("write_file", "patch_file", "deploy_patch") and original_filename in IMMUTABLE_FILES:
            return PermissionResult.deny(
                f"IMMUTABLE: {original_filename} cannot be modified under any circumstance",
                tier=PermissionTier.ELEVATED
            )

        # 2. Sensitive files — block reads
        if action == "read_file" and filename in {f.lower() for f in SENSITIVE_FILES}:
            return PermissionResult.deny(
                f"SENSITIVE: {original_filename} contains credentials. Use os.getenv() instead.",
                tier=PermissionTier.READ_ONLY
            )

        # 3. Deploy-only files — redirect write/patch to deploy_patch
        if action in ("write_file", "patch_file") and original_filename in DEPLOY_ONLY_FILES:
            service = FILE_TO_SERVICE.get(original_filename, "worker")
            return PermissionResult.deny(
                f"System file {original_filename} requires deploy_patch (service={service})",
                tier=PermissionTier.ELEVATED,
                redirect_action="deploy_patch",
                redirect_service=service,
            )

        # 4. System file write protection — block tiny writes
        if action == "write_file" and original_filename in SYSTEM_FILES and content:
            if len(content) < 200:
                return PermissionResult.deny(
                    f"Write blocked: {original_filename} is a core system file and new content is only {len(content)} bytes (minimum 200)",
                    tier=PermissionTier.ELEVATED
                )

        # 5. Path validation — must be within SAFE_PATHS
        if action in ("read_file", "write_file", "patch_file") and target:
            try:
                resolved = Path(target).resolve()
                if not any(self._is_under(resolved, safe) for safe in SAFE_PATHS):
                    return PermissionResult.deny(
                        f"Path {target} is outside allowed directories",
                        tier=PermissionTier.WORKSPACE_WRITE
                    )
            except Exception:
                pass  # Let other validation handle bad paths

        # All checks passed
        return PermissionResult.allow()

    def get_tier(self, action: str) -> PermissionTier:
        """Get the permission tier required for an action."""
        return ACTION_TIERS.get(action, PermissionTier.WORKSPACE_WRITE)

    def is_elevated(self, action: str) -> bool:
        """Check if an action requires elevated permissions."""
        return self.get_tier(action) == PermissionTier.ELEVATED

    @staticmethod
    def _is_under(path: Path, parent: Path) -> bool:
        """Check if path is under parent directory."""
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


# Module-level singleton
policy = PermissionPolicy()
