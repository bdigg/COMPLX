import json
import os
from datetime import datetime

class StatusManager:
    def __init__(self, filepath="status.json"):
        self.filepath = filepath
        self.status = {
            "state": "Idle",
            "timestamp": datetime.now().isoformat(),
            "message": ""
        }
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self.status = json.load(f)
            except:
                pass

    def set_status(self, state, message=""):
        self.status["state"] = state
        self.status["message"] = message
        self.status["timestamp"] = datetime.now().isoformat()
        self._save()

    def get_status(self):
        return self.status

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump(self.status, f)
