"""JSON lines on stdout, collected by Loki. Never log message text, tokens or addresses."""

import logging
import sys

import structlog

_shared = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
]


def setup(level: str = "INFO") -> None:
    structlog.configure(
        processors=[*_shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.EventRenamer("msg"),
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(max(logging.getLevelName(level.upper()), logging.INFO))  # DEBUG is for our own code only
    logging.getLogger("pantry").setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
