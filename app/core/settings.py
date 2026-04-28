import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT_DIR / "runtime"
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
FRONTEND_INDEX_PATH = FRONTEND_DIST_DIR / "index.html"
VERSION_PATH = ROOT_DIR / "VERSION"


def _resolve_app_version() -> str:
    env_value = os.getenv("MAKERHUB_APP_VERSION", "").strip()
    if env_value:
      return env_value
    if VERSION_PATH.exists():
      return VERSION_PATH.read_text(encoding="utf-8").strip() or "0.1.0"
    return "0.1.0"


APP_VERSION = _resolve_app_version()


def _resolve_process_role() -> str:
    value = (
        os.getenv("MAKERHUB_PROCESS_ROLE", "")
        or os.getenv("MAKERHUB_ROLE", "")
        or ""
    ).strip().lower()
    return value or "legacy"


def _resolve_bool(env_name: str, default: bool) -> bool:
    raw = str(os.getenv(env_name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


PROCESS_ROLE = _resolve_process_role()
_ROLE_DEFAULT_BACKGROUND = PROCESS_ROLE in {"legacy", "worker", "background", "all"}
BACKGROUND_TASKS_ENABLED = _resolve_bool(
    "MAKERHUB_BACKGROUND_TASKS",
    _resolve_bool("MAKERHUB_RUN_BACKGROUND_TASKS", _ROLE_DEFAULT_BACKGROUND),
)


def _resolve_dir(env_name: str, fallback: Path) -> Path:
    raw = str(os.getenv(env_name, "")).strip()
    if raw:
        return Path(raw).expanduser()
    return fallback


def _resolve_int(env_name: str, fallback: int) -> int:
    raw = str(os.getenv(env_name, "")).strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


CONFIG_DIR = _resolve_dir("MAKERHUB_CONFIG_DIR", RUNTIME_DIR / "config")
LOGS_DIR = _resolve_dir("MAKERHUB_LOGS_DIR", RUNTIME_DIR / "logs")
STATE_DIR = _resolve_dir("MAKERHUB_STATE_DIR", RUNTIME_DIR / "state")
ARCHIVE_DIR = _resolve_dir("MAKERHUB_ARCHIVE_DIR", RUNTIME_DIR / "archive")
LOCAL_DIR = _resolve_dir("MAKERHUB_LOCAL_DIR", RUNTIME_DIR / "local")
MAX_MANUAL_ATTACHMENT_BYTES = _resolve_int("MAKERHUB_MAX_MANUAL_ATTACHMENT_BYTES", 128 * 1024 * 1024)

CONFIG_PATH = CONFIG_DIR / "config.json"


def ensure_app_dirs() -> None:
    for path in (CONFIG_DIR, LOGS_DIR, STATE_DIR, ARCHIVE_DIR, LOCAL_DIR):
        path.mkdir(parents=True, exist_ok=True)
