"""
Shared logging utility for all services.
Writes to both console AND log files so the dashboard can display them.
All output is redacted for credentials before hitting disk or console.
"""

import logging
import sys
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# Late imports to avoid circular deps — loaded on first format call
_redact_fn = None
_trace_fn = None

def _get_redact():
    global _redact_fn
    if _redact_fn is None:
        from shared.redact import redact
        _redact_fn = redact
    return _redact_fn

def _get_trace_id():
    global _trace_fn
    if _trace_fn is None:
        from shared.trace import get_trace_id
        _trace_fn = get_trace_id
    return _trace_fn()


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs credentials and injects trace IDs into every log message."""

    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)

    def format(self, record):
        # Inject trace ID if available
        trace_id = _get_trace_id()
        if trace_id:
            record.msg = f"[{trace_id}] {record.msg}"

        # Redact the message itself
        record.msg = _get_redact()(str(record.msg))
        # Redact any string args
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _get_redact()(str(v)) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _get_redact()(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return super().format(record)


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(service_name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = RedactingFormatter(
        '[%(asctime)s] %(name)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    log_file = LOGS_DIR / f"{service_name}.log"
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_format = RedactingFormatter(
        '[%(asctime)s] %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger

def clear_log(service_name: str):
    log_file = LOGS_DIR / f"{service_name}.log"
    if log_file.exists():
        log_file.write_text("")

def read_log(service_name: str, lines: int = 50) -> str:
    log_file = LOGS_DIR / f"{service_name}.log"
    if not log_file.exists():
        return f"No log file found for {service_name}"
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
            return ''.join(all_lines[-lines:])
    except Exception as e:
        return f"Error reading log: {e}"
