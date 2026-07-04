import json
import logging
import os
import re
import sys
from datetime import datetime
from time import time

from ..config.settings import settings
from ..runtime_update import is_update_available

_LOG_FORMAT: str | None = None

# Before huggingface_hub/transformers import, suppress download/load tqdm bars.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def suppress_ml_progress_bars() -> None:
    """Hide Hugging Face / transformers tqdm progress bars; keep app INFO load logs."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        from huggingface_hub.utils import disable_progress_bars

        disable_progress_bars()
    except Exception:
        pass
    try:
        from transformers.utils.logging import disable_progress_bar

        disable_progress_bar()
    except Exception:
        pass

# Route uvicorn loggers through the root handler configured by configure_logging().
UVICORN_LOG_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {"level": "INFO", "propagate": True},
        "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.asgi": {"level": "INFO", "propagate": True},
    },
}


def get_uvicorn_log_config() -> dict[str, object]:
    """Return a uvicorn log_config dict that uses Topos root logging formatters."""
    return UVICORN_LOG_CONFIG


def align_uvicorn_loggers() -> None:
    """Ensure uvicorn access/error logs use the root logger formatters."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def configure_logging() -> None:
    """Configure logging to stdout based on environment/log settings."""
    suppress_ml_progress_bars()
    handler = logging.StreamHandler(sys.stdout)

    def resolve_format() -> str:
        if settings.log_format:
            return settings.log_format.lower()
        if settings.environment.lower() in {"prod", "production"}:
            return "json"
        return "color"

    log_format = resolve_format()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    elif log_format == "color" and sys.stdout.isatty():
        handler.setFormatter(ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    root = logging.getLogger()
    # Set log level from settings
    log_level_str = settings.log_level.upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    root.setLevel(log_level)
    root.handlers = [handler]
    
    # Suppress WebSocket ping/pong keepalive messages
    # These are noisy debug logs from the websockets library
    websockets_logger = logging.getLogger("websockets")
    websockets_logger.setLevel(logging.INFO)  # Only show INFO and above, suppress DEBUG

    # transformers / huggingface_hub use urllib3; at DEBUG root these lines break tqdm/progress output
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
    # httpx/httpcore DEBUG emits per-request socket chatter; keep UMA transform logs readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # transformers logs harmless weight-load reports (e.g. UNEXPECTED pooler keys for NER models).
    try:
        from transformers.utils import logging as transformers_logging

        transformers_logging.set_verbosity_error()
    except ImportError:
        pass

    global _LOG_FORMAT
    _LOG_FORMAT = log_format


def log_request_timing(logger: logging.Logger, path: str, status_code: int, started_at: float) -> None:
    elapsed_ms = int((time() - started_at) * 1000)
    if _LOG_FORMAT == "json":
        logger.info(
            "request.complete",
            extra={"extra": {"path": path, "status": status_code, "latency_ms": elapsed_ms}},
        )
    else:
        logger.info(
            "request.complete path=%s status=%s latency_ms=%s",
            path,
            status_code,
            elapsed_ms,
        )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": record.created,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            payload.update(record.extra)
        return json.dumps(payload)


class ColorFormatter(logging.Formatter):
    """Color formatter matching Pipecat-style logs with text colors (no backgrounds)."""
    
    _RESET = "\x1b[0m"
    
    # Text colors for log levels (no backgrounds)
    _LEVEL_COLORS = {
        logging.DEBUG: "\x1b[38;5;26m",      # Light blue (matching DEBUG in image)
        logging.INFO: "\x1b[38;5;220m",     # Light yellow/gold (matching INFO in image)
        logging.WARNING: "\x1b[38;5;208m",  # Orange (matching WARNING in image)
        logging.ERROR: "\x1b[38;5;196m",    # Red
        logging.CRITICAL: "\x1b[38;5;196m", # Red
    }
    
    # Timestamp color: Forest green (amber when a newer Topos release is on PyPI)
    _TIMESTAMP_COLOR = "\x1b[38;5;28m"  # Forest green
    _TIMESTAMP_UPDATE_COLOR = "\x1b[38;5;214m"  # Amber
    
    # Separator color: Light gray
    _SEPARATOR_COLOR = "\x1b[38;5;244m"  # Light gray
    
    # Class instance names: Teal/cyan-like blue-green (slightly more green/blue than debug blue)
    _CLASS_NAME_COLOR = "\x1b[38;5;26m"  # Teal/cyan blue-green
    
    # Pipeline stage tags: Blue color
    _PIPELINE_TAG_COLOR = "\x1b[38;5;75m"  # Blue (matching user request)
    
    # Message text: Same blue as DEBUG
    _MESSAGE_COLOR = "\x1b[38;5;26m"  # Light blue (same as DEBUG)

    # source / source_id values: bold magenta — easy to scan in pipeline logs
    _SOURCE_IDENTITY_VALUE_COLOR = "\x1b[1;38;5;213m"  # Bold bright magenta
    
    # Logger name (module path): Same teal/cyan as class names
    _LOGGER_NAME_COLOR = "\x1b[38;5;75m"  # Teal/cyan blue-green
    
    _ARROW_COLOR = "\x1b[38;5;244m"  # Gray for arrows

    _SOURCE_KV_PATTERN = re.compile(
        r"(?<![?&/])"  # skip URL query/path fragments like ...&source=editors
        r"\b(?P<key>source_id|source)(?P<sep>=|:)"
        r"(?:"
        r"(?P<q>['\"])(?P<qval>[^'\"]+)(?P=q)|"
        r"(?P<uval>[^\s,;)\]&]+)"
        r")"
    )

    _SOURCE_VALUE_SKIP = frozenset({"%s", "%d", "%i", "%f", "?"})

    def _highlight_source_identities(self, message: str) -> str:
        """Bold magenta on source / source_id values (keys stay default message color)."""

        def repl(match: re.Match[str]) -> str:
            value = match.group("qval") or match.group("uval") or ""
            if value in self._SOURCE_VALUE_SKIP or value.startswith("excluded."):
                return match.group(0)
            key = match.group("key")
            sep = match.group("sep")
            quote = match.group("q") or ""
            colored_value = (
                f"{self._SOURCE_IDENTITY_VALUE_COLOR}{value}{self._RESET}{self._MESSAGE_COLOR}"
            )
            return f"{key}{sep}{quote}{colored_value}{quote}"

        return self._SOURCE_KV_PATTERN.sub(repl, message)

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        # Format timestamp: YYYY-MM-DD HH:MM:SS.ms (green)
        timestamp_str = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        timestamp_color = (
            self._TIMESTAMP_UPDATE_COLOR if is_update_available() else self._TIMESTAMP_COLOR
        )
        timestamp = f"{timestamp_color}{timestamp_str}{self._RESET}"
        
        # Get text color for log level (no background)
        level_color = self._LEVEL_COLORS.get(record.levelno, "")
        level = record.levelname
        if level_color:
            level = f"{level_color}{level}{self._RESET}"
        
        # Color logger name (module path) in teal/cyan
        logger_name = record.name
        logger_name_colored = f"{self._LOGGER_NAME_COLOR}{logger_name}{self._RESET}"
        
        # Get message and extract pipeline tag if present
        message = record.getMessage()

        # Extract pipeline stage tag from message if it exists
        pipeline_tag_pattern = r'\[PIPELINE:[A-Z_]+\]'
        pipeline_tag_match = re.search(pipeline_tag_pattern, message)
        pipeline_tag_display = ""
        if pipeline_tag_match:
            # Extract the tag and remove it from the message
            pipeline_tag = pipeline_tag_match.group(0)
            message = re.sub(pipeline_tag_pattern + r'\s*', '', message, count=1)  # Remove tag and any following space
            # Color the pipeline tag
            pipeline_tag_display = f" {self._PIPELINE_TAG_COLOR}{pipeline_tag}{self._RESET}"
        
        # Apply colors to message
        # First, wrap the entire message in blue (default message color)
        message_colored = f"{self._MESSAGE_COLOR}{message}{self._RESET}"
        
        # Then apply special colors to specific parts (these will override the blue)
        # Color class instance names (pattern: ClassName#N)
        # Match class names like IngestionManager#1, Emo27Job#1, etc.
        class_name_pattern = r'\b([A-Z][a-zA-Z0-9_]*#\d+)\b'
        message_colored = re.sub(
            class_name_pattern,
            f"{self._CLASS_NAME_COLOR}\\1{self._RESET}{self._MESSAGE_COLOR}",
            message_colored
        )
        
        # Color arrows (→ and ←) for pipeline flow
        message_colored = re.sub(
            r'([→←])',
            f"{self._ARROW_COLOR}\\1{self._RESET}{self._MESSAGE_COLOR}",
            message_colored
        )

        message_colored = self._highlight_source_identities(message_colored)
        
        # Format: TIMESTAMP | LEVEL | [PIPELINE:TAG] | LOGGER: MESSAGE
        separator = f"{self._SEPARATOR_COLOR}|{self._RESET}"
        if pipeline_tag_display:
            return f"{timestamp} {separator} {level} {separator}{pipeline_tag_display} {separator} {logger_name_colored}: {message_colored}"
        else:
            return f"{timestamp} {separator} {level} {separator} {logger_name_colored}: {message_colored}"
