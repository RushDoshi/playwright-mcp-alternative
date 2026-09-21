"""Structured run logging (Layer A) — mirrors the prototype's Run Log."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

_CONFIGURED = False


def configure(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
    root = logging.getLogger("quality_os")
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"quality_os.{name}")


# Bounded, in-memory tail of each run's log lines. This exists so a UI can poll
# GET /runs/{id}/logs; it is deliberately not the source of truth (stdout and the
# state file are), so losing it on restart costs nothing.
_MAX_LINES_PER_RUN = 500
_run_logs: dict[str, list[dict[str, str]]] = {}


def run_log(run_id: str, actor: str, message: str) -> dict[str, str]:
    """Produce a structured log line like the prototype's '[ACTOR] message'."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "actor": actor,
        "message": message,
    }
    get_logger("run").info("[%s] %s", actor, message)
    buf = _run_logs.setdefault(run_id, [])
    buf.append(entry)
    if len(buf) > _MAX_LINES_PER_RUN:
        del buf[:-_MAX_LINES_PER_RUN]
    return entry


def get_run_logs(run_id: str, since: int = 0) -> list[dict[str, str]]:
    """Return log entries for a run from index ``since`` onward."""
    return _run_logs.get(run_id, [])[max(since, 0):]
