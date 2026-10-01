"""Keys and settings for the router.

Two API keys, loaded the same way:

  CEREBRAS_API_KEY   env var, else ~/.config/llm-popup/key
  TYPESAFE_API_KEY   env var, else ~/.config/llm-popup/typesafe_key

A .env file is read into the environment at startup (without overriding
variables that are already set), so keys kept in the repo's .env work when
running from source. The built app reads ~/.config/llm-popup/.env instead.
No python-dotenv dependency: the format we need is KEY=value, one per line,
optional quotes, # comments.
"""

import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "llm-popup"
TYPESAFE_KEY_FILE = CONFIG_DIR / "typesafe_key"

# Router settings, all overridable from the environment (or .env).
ROUTER_TIMEOUT = float(os.environ.get("QUICKASK_ROUTER_TIMEOUT", "1.0"))      # seconds, hard
SCOPE_THRESHOLD = float(os.environ.get("QUICKASK_SCOPE_THRESHOLD", "0.6"))    # min scope confidence
FRONTIER_THRESHOLD = float(os.environ.get("QUICKASK_FRONTIER_THRESHOLD", "0.7"))
JEV_MODEL = os.environ.get("QUICKASK_JEV_MODEL", "jev-latest")


def project_root():
    return Path(__file__).resolve().parent.parent


def default_env_file():
    if getattr(sys, "frozen", False):
        return CONFIG_DIR / ".env"
    return project_root() / ".env"


def parse_dotenv(text):
    """KEY=value lines -> dict. Quotes around the value are stripped, blank
    lines and # comments skipped, malformed lines ignored."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path=None):
    """Put the file's variables into os.environ unless already set there.
    Returns the names that were set. Missing file: nothing happens."""
    path = Path(path) if path else default_env_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    added = []
    for key, value in parse_dotenv(text).items():
        if key not in os.environ:
            os.environ[key] = value
            added.append(key)
    return added


def load_typesafe_key():
    """Environment variable first, then the key file. None if neither."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and TYPESAFE_KEY_FILE.exists():
        key = TYPESAFE_KEY_FILE.read_text().strip()
    return key or None


def _refresh_settings():
    """Re-read the tunables after load_dotenv(), since the module-level
    values were computed at import time from the environment alone."""
    global ROUTER_TIMEOUT, SCOPE_THRESHOLD, FRONTIER_THRESHOLD, JEV_MODEL
    ROUTER_TIMEOUT = float(os.environ.get("QUICKASK_ROUTER_TIMEOUT", ROUTER_TIMEOUT))
    SCOPE_THRESHOLD = float(os.environ.get("QUICKASK_SCOPE_THRESHOLD", SCOPE_THRESHOLD))
    FRONTIER_THRESHOLD = float(os.environ.get("QUICKASK_FRONTIER_THRESHOLD", FRONTIER_THRESHOLD))
    JEV_MODEL = os.environ.get("QUICKASK_JEV_MODEL", JEV_MODEL)


def setup(env_file=None):
    """Call once at startup: read .env, then recompute the settings."""
    added = load_dotenv(env_file)
    _refresh_settings()
    return added
