"""
RouxYou — Hook Pipeline (Phase 36)
====================================
Pre/post tool execution hooks. Extensible safety checks without modifying Worker.

Hook lifecycle:
    1. Worker receives action (e.g., write_file)
    2. HookRunner.run_pre_hooks(action, context) → HookResult
    3. If denied: Worker skips execution, returns hook's message
    4. Worker executes action
    5. HookRunner.run_post_hooks(action, context, result) → modified result

Built-in hooks (extracted from worker.py and orchestrator.py):
    - PermissionHook: checks shared/permissions.py policy
    - CredentialRedactionHook: redacts secrets from outputs
    - TruncationGuardHook: blocks writes that shrink files >50%

Custom hooks can be registered at runtime without modifying Worker.

Usage:
    from shared.hooks import hook_runner

    # Pre-execution check:
    result = hook_runner.run_pre_hooks("write_file", {"target": "worker.py", "content": "..."})
    if not result.allowed:
        return {"success": False, "error": result.message}

    # Post-execution processing:
    output = hook_runner.run_post_hooks("read_file", {"target": "config.py"}, raw_output)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any

logger = logging.getLogger("roux.hooks")


# ============================================================
# Hook Types
# ============================================================

class HookEvent(Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"


@dataclass
class HookResult:
    """Result from a hook execution."""
    allowed: bool = True
    message: str = ""
    modified_output: Optional[Any] = None  # For post-hooks that transform output
    hook_name: str = ""

    @staticmethod
    def allow() -> "HookResult":
        return HookResult(allowed=True)

    @staticmethod
    def deny(message: str, hook_name: str = "") -> "HookResult":
        return HookResult(allowed=False, message=message, hook_name=hook_name)


# ============================================================
# Hook ABC
# ============================================================

class Hook(ABC):
    """Base class for all hooks."""

    name: str = "base_hook"
    event: HookEvent = HookEvent.PRE_TOOL_USE
    priority: int = 0  # Lower = runs first

    @abstractmethod
    def execute(self, action: str, context: dict) -> HookResult:
        """
        Execute the hook.

        Args:
            action: The tool action (read_file, write_file, etc.)
            context: Dict with action-specific data:
                - target: file path or service name
                - content: content being written (for write/patch)
                - result: execution result (for post-hooks)
        """
        ...


# ============================================================
# Built-in Pre-Hooks
# ============================================================

class PermissionHook(Hook):
    """
    Checks the centralized permission policy before tool execution.
    Replaces scattered IMMUTABLE_FILES, DEPLOY_ONLY_FILES, SENSITIVE_FILES checks in worker.py.
    """
    name = "permission_check"
    event = HookEvent.PRE_TOOL_USE
    priority = 0  # Runs first

    def execute(self, action: str, context: dict) -> HookResult:
        from shared.permissions import policy

        target = context.get("target", "")
        content = context.get("content", "")

        result = policy.check(action, target=target, content=content)

        if not result.allowed:
            logger.warning(f"HOOK [{self.name}]: DENIED {action} on {target}: {result.reason}")

            # Include redirect info if available
            msg = result.reason
            if result.redirect_action:
                msg += f" | Redirect: use {result.redirect_action}"
                if result.redirect_service:
                    msg += f" (service={result.redirect_service})"

            return HookResult.deny(msg, hook_name=self.name)

        return HookResult.allow()


class TruncationGuardHook(Hook):
    """
    Blocks writes that would shrink a file by more than 50%.
    Phase 17: "Don't Nuke Yourself" circuit breaker.
    """
    name = "truncation_guard"
    event = HookEvent.PRE_TOOL_USE
    priority = 10  # After permission check

    def execute(self, action: str, context: dict) -> HookResult:
        if action != "write_file":
            return HookResult.allow()

        target = context.get("target", "")
        content = context.get("content", "")

        if not target or not content:
            return HookResult.allow()

        from pathlib import Path
        path = Path(target)
        if not path.exists():
            return HookResult.allow()  # New file, no truncation possible

        try:
            existing_size = path.stat().st_size
            new_size = len(content.encode("utf-8"))

            if existing_size > 500 and new_size < existing_size * 0.5:
                pct = int(new_size / existing_size * 100)
                msg = (
                    f"TRUNCATION BLOCKED: {path.name} would shrink from "
                    f"{existing_size} to {new_size} bytes ({pct}% of original). "
                    f"Use patch_file for targeted edits instead of full rewrite."
                )
                logger.warning(f"HOOK [{self.name}]: {msg}")
                return HookResult.deny(msg, hook_name=self.name)
        except Exception:
            pass

        return HookResult.allow()


# ============================================================
# Built-in Post-Hooks
# ============================================================

class CredentialRedactionHook(Hook):
    """
    Redacts credential-like patterns from tool outputs.
    Replaces _redact() in orchestrator.py and redact_credentials() in worker.py.
    """
    name = "credential_redaction"
    event = HookEvent.POST_TOOL_USE
    priority = 0  # Runs first on output

    def execute(self, action: str, context: dict) -> HookResult:
        from shared.permissions import redact_credentials

        result = context.get("result", {})
        if not isinstance(result, dict):
            return HookResult.allow()

        modified = False

        # Redact common output fields
        for key in ("content", "response", "error", "output", "summary"):
            if key in result and isinstance(result[key], str):
                redacted = redact_credentials(result[key])
                if redacted != result[key]:
                    result[key] = redacted
                    modified = True

        if modified:
            logger.debug(f"HOOK [{self.name}]: Redacted credentials from {action} output")
            return HookResult(allowed=True, modified_output=result, hook_name=self.name)

        return HookResult.allow()


# ============================================================
# Hook Runner
# ============================================================

class HookRunner:
    """
    Manages and executes hooks in priority order.
    Thread-safe for concurrent use across services.
    """

    def __init__(self):
        self._pre_hooks: list[Hook] = []
        self._post_hooks: list[Hook] = []

    def register(self, hook: Hook):
        """Register a hook. Automatically sorted by priority."""
        if hook.event == HookEvent.PRE_TOOL_USE:
            self._pre_hooks.append(hook)
            self._pre_hooks.sort(key=lambda h: h.priority)
        else:
            self._post_hooks.append(hook)
            self._post_hooks.sort(key=lambda h: h.priority)
        logger.info(f"Hook registered: {hook.name} ({hook.event.value}, priority={hook.priority})")

    def run_pre_hooks(self, action: str, context: dict) -> HookResult:
        """
        Run all pre-execution hooks. Stops on first denial.

        Returns:
            HookResult — .allowed=True if all hooks pass, False if any denies.
        """
        for hook in self._pre_hooks:
            try:
                result = hook.execute(action, context)
                if not result.allowed:
                    return result
            except Exception as e:
                logger.error(f"Hook {hook.name} crashed: {e}")
                # Hook crashes don't block execution — fail open
                continue

        return HookResult.allow()

    def run_post_hooks(self, action: str, context: dict, result: dict) -> dict:
        """
        Run all post-execution hooks. Can modify the result.

        Args:
            action: The tool action
            context: Original action context
            result: The execution result dict

        Returns:
            The (possibly modified) result dict
        """
        ctx = dict(context)
        ctx["result"] = result

        for hook in self._post_hooks:
            try:
                hook_result = hook.execute(action, ctx)
                if hook_result.modified_output is not None:
                    result = hook_result.modified_output
                    ctx["result"] = result
            except Exception as e:
                logger.error(f"Post-hook {hook.name} crashed: {e}")
                continue

        return result

    @property
    def registered_hooks(self) -> list[str]:
        """List registered hook names."""
        return (
            [f"pre:{h.name}" for h in self._pre_hooks] +
            [f"post:{h.name}" for h in self._post_hooks]
        )


# ============================================================
# Module-level singleton with default hooks
# ============================================================

hook_runner = HookRunner()

# Register built-in hooks
hook_runner.register(PermissionHook())
hook_runner.register(TruncationGuardHook())
hook_runner.register(CredentialRedactionHook())
