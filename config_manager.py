import json
import os
from pathlib import Path

class ConfigManager:
    def __init__(self, base_dir="configs"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, category, name):
        return self.base_dir / category / f"{name}.json"

    def save_config(self, category, name, data):
        path = self._get_path(category, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_config(self, category, name):
        path = self._get_path(category, name)
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def list_configs(self, category):
        path = self.base_dir / category
        if path.exists():
            return sorted(f.stem for f in path.glob("*.json"))
        return []

    def delete_config(self, category, name):
        path = self._get_path(category, name)
        if path.exists():
            path.unlink()
