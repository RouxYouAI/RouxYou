"""
Centralized credential redaction for all RouxYou services.
Single source of truth — every module imports from here.
"""

import re
from typing import Any, Dict

CREDENTIAL_PATTERNS = [
    re.compile(r'(?i)(TOKEN|PASSWORD|SECRET|API_KEY|APIKEY|AUTH|CREDENTIAL|PRIVATE_KEY)\s*[=:]\s*\S+'),
    re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*'),
    re.compile(r'(?<=[=:\s])[A-Za-z0-9+/\-._]{40,}={0,3}'),
]


def redact(text: str) -> str:
    """Redact credentials from a string."""
    if not isinstance(text, str):
        text = str(text)
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_dict(d: Any, max_depth: int = 4) -> Any:
    """Recursively redact all string values in a dict/list structure."""
    if max_depth <= 0:
        return d
    if isinstance(d, str):
        return redact(d)
    if isinstance(d, dict):
        return {k: redact_dict(v, max_depth - 1) for k, v in d.items()}
    if isinstance(d, list):
        return [redact_dict(item, max_depth - 1) for item in d]
    return d
