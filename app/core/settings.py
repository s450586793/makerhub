import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT_DIR / "runtime"


def _resolve_dir(env_name: str, fallback: Path) -> Path:
    raw = str(os.getenv(env_name, "")).strip()
    if raw:
        return Path(raw).expanduser()
    return fallback


CONFIG_DIR = _resolve_dir("MAKERHUB_CONFIG_DIR", RUNTIME_DIR / "config")
LOGS_DIR = _resolve_dir("MAKERHUB_LOGS_DIR", RUNTIME_DIR / "logs")
STATE_DIR = _resolve_dir("MAKERHUB_STATE_DIR", RUNTIME_DIR / "state")
ARCHIVE_DIR = _resolve_dir("MAKERHUB_ARCHIVE_DIR", RUNTIME_DIR / "archive")
LOCAL_DIR = _resolve_dir("MAKERHUB_LOCAL_DIR", RUNTIME_DIR / "local")

CONFIG_PATH = CONFIG_DIR / "config.json"


def ensure_app_dirs() -> None:
    for path in (CONFIG_DIR, LOGS_DIR, STATE_DIR, ARCHIVE_DIR, LOCAL_DIR):
        path.mkdir(parents=True, exist_ok=True)
