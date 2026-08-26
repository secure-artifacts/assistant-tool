import sys
from pathlib import Path


def _resolve_app_root() -> Path:
    """Return the directory that should hold user-managed runtime files."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _resolve_app_root()
