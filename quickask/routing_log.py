"""Shadow-mode log: one JSON line per query, whatever the checkbox said.

The point is to review a week of routing decisions before trusting the
router with anything faster than "answer then log". Every line records what
Jev said and what the popup actually did, so the two can be compared.

Location: logs/routing.jsonl in the repo when running from source, and
~/.config/llm-popup/logs/routing.jsonl for the built app. QUICKASK_LOG_FILE
overrides both.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import config

# Field order of a log line, for readers and for review_log's table.
FIELDS = (
    "timestamp", "question", "app_name", "window_title", "use_jev",
    "scope", "scope_confidence", "needs_frontier_prob", "latency_ms",
    "path", "error", "probabilities", "model", "scope_threshold", "follow_up",
)


def default_log_file():
    override = os.environ.get("QUICKASK_LOG_FILE")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return config.CONFIG_DIR / "logs" / "routing.jsonl"
    return config.project_root() / "logs" / "routing.jsonl"


def make_entry(question, desktop_state, use_jev, result, path, follow_up=False, now=None):
    """The dict for one line. `result` is a RoutingResult; `path` is what the
    popup did: "default" or "scope:<name>"."""
    return {
        "timestamp": (now or datetime.now()).isoformat(timespec="seconds"),
        "question": question,
        "app_name": desktop_state.app_name if desktop_state else None,
        "window_title": desktop_state.window_title if desktop_state else None,
        "use_jev": bool(use_jev),
        "scope": result.scope,
        "scope_confidence": round(result.scope_confidence, 4),
        "needs_frontier_prob": round(result.needs_frontier_prob, 4),
        "latency_ms": result.latency_ms,
        "path": path,
        "error": result.error,
        "probabilities": result.probabilities,
        "model": result.model,
        "scope_threshold": config.SCOPE_THRESHOLD,
        "follow_up": bool(follow_up),
    }


def append(entry, path=None):
    """Append one line. Never raises: a logging failure is printed to stderr
    and otherwise ignored, because the log must never break the popup."""
    path = Path(path) if path else default_log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"routing log: could not write {path}: {e}", file=sys.stderr)
        return False


def read(path=None, last=None):
    """-> list of entries (dicts), oldest first. Lines that fail to parse are
    skipped with a note on stderr. `last` keeps only the newest N."""
    path = Path(path) if path else default_log_file()
    if not path.exists():
        return []
    entries = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"routing log: skipping line {i}: {e}", file=sys.stderr)
    return entries[-last:] if last else entries
