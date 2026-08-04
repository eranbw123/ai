"""Shared helpers used by both claude_export.py and chatgpt_export.py."""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def load_env_local():
    env_path = REPO_ROOT / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        os.environ.setdefault(key, value)


def parse_date(value):
    """Parse a YYYY-MM-DD (or full ISO) string into a timezone-aware datetime."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"Invalid date '{value}', expected YYYY-MM-DD")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name or "untitled")[:150]
