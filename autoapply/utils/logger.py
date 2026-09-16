"""
Structured rotating logger for AutoAppy.
Writes both to console (via rich) and to a rotating log file.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name: str = "autoapply", log_dir: str = "logs") -> logging.Logger:
    """
    Get a named logger with rotating file handler + optional console handler.

    Args:
        name: Logger name (default: "autoapply")
        log_dir: Directory for log files

    Returns:
        Configured logging.Logger instance
    """
    logger = logging.getLogger(name)

    # Don't add handlers if already configured
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Ensure log directory exists
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # ── Rotating File Handler ──────────────────────────────────────────────────
    # Max 5 MB per file, keep last 5 files
    log_file = Path(log_dir) / "autoapply.log"
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)

    # Structured format for log files
    file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger


# Module-level default logger
log = get_logger()


def log_pipeline_start(run_id: int, dry_run: bool = False):
    """Log pipeline start event."""
    log.info(f"Pipeline started | run_id={run_id} | dry_run={dry_run}")


def log_discovery(source: str, count: int, elapsed_ms: int = 0):
    """Log job discovery result from a source."""
    log.info(f"Discovery | source={source} | jobs={count} | elapsed_ms={elapsed_ms}")


def log_scoring(job_id: int, company: str, title: str, score: float, verdict: str):
    """Log a scored job."""
    log.info(
        f"Scored | job_id={job_id} | company={company!r} | title={title!r} "
        f"| score={score:.1f} | verdict={verdict}"
    )


def log_application(job_id: int, company: str, title: str, method: str, success: bool, reason: str = ""):
    """Log an application attempt."""
    status = "SUCCESS" if success else "FAILED"
    log.info(
        f"Application | job_id={job_id} | company={company!r} | title={title!r} "
        f"| method={method} | status={status} | reason={reason!r}"
    )


def log_llm_call(provider: str, tokens_in: int = 0, tokens_out: int = 0, success: bool = True):
    """Log an LLM API call."""
    status = "ok" if success else "failed"
    log.debug(
        f"LLM | provider={provider} | tokens_in={tokens_in} | tokens_out={tokens_out} | status={status}"
    )


def log_error(context: str, error: Exception):
    """Log an error with full traceback."""
    log.error(f"Error | context={context} | {type(error).__name__}: {error}", exc_info=True)


def log_pipeline_end(run_id: int, discovered: int, scored: int, applied: int, status: str):
    """Log pipeline completion summary."""
    log.info(
        f"Pipeline complete | run_id={run_id} | discovered={discovered} "
        f"| scored={scored} | applied={applied} | status={status}"
    )
