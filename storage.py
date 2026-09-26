"""Tiny JSON-file store for subscribers and the last signal sent."""

import json
import os


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.data = {"subscribers": [], "last_signal": None}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def subscribers(self) -> list[int]:
        return list(self.data["subscribers"])

    def subscribe(self, chat_id: int) -> bool:
        if chat_id in self.data["subscribers"]:
            return False
        self.data["subscribers"].append(chat_id)
        self._save()
        return True

    def unsubscribe(self, chat_id: int) -> bool:
        if chat_id not in self.data["subscribers"]:
            return False
        self.data["subscribers"].remove(chat_id)
        self._save()
        return True

    @property
    def last_signal(self) -> dict | None:
        return self.data["last_signal"]

    def set_last_signal(self, signal: dict):
        self.data["last_signal"] = signal
        self._save()
