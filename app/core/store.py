import json
from pathlib import Path

from app.core.settings import CONFIG_PATH, DATA_DIR
from app.schemas.models import AppConfig


class JsonStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(AppConfig())

    def load(self) -> AppConfig:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(payload)

    def save(self, config: AppConfig) -> AppConfig:
        self.path.write_text(
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return config

