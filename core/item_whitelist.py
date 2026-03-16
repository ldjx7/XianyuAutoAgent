import json
import os
import threading
from typing import Iterable, List, Set

try:
    from loguru import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


def _normalize_item_id(item_id):
    if item_id is None:
        return None
    text = str(item_id).strip()
    return text or None


def _normalize_item_ids(item_ids: Iterable[str]) -> List[str]:
    normalized: Set[str] = set()
    for item_id in item_ids:
        text = _normalize_item_id(item_id)
        if text:
            normalized.add(text)
    return sorted(normalized)


class ItemWhitelistStore:
    def __init__(self, file_path: str, env_item_ids: str = ""):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._env_item_ids = set(_normalize_item_ids((env_item_ids or "").split(",")))
        self._file_item_ids: Set[str] = set()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._file_item_ids = set(self._load_file_item_ids())

    def list_items(self) -> List[str]:
        with self._lock:
            return sorted(self._env_item_ids | self._file_item_ids)

    def list_env_items(self) -> List[str]:
        with self._lock:
            return sorted(self._env_item_ids)

    def list_file_items(self) -> List[str]:
        with self._lock:
            return sorted(self._file_item_ids)

    def is_allowed(self, item_id: str) -> bool:
        normalized = _normalize_item_id(item_id)
        if not normalized:
            return False
        with self._lock:
            return normalized in self._env_item_ids or normalized in self._file_item_ids

    def add_item(self, item_id: str) -> List[str]:
        normalized = _normalize_item_id(item_id)
        if not normalized:
            raise ValueError("item_id is required")
        with self._lock:
            self._file_item_ids.add(normalized)
            self._persist_locked()
            return self.list_items()

    def remove_item(self, item_id: str) -> List[str]:
        normalized = _normalize_item_id(item_id)
        if not normalized:
            raise ValueError("item_id is required")
        with self._lock:
            self._file_item_ids.discard(normalized)
            self._persist_locked()
            return self.list_items()

    def replace_items(self, item_ids: Iterable[str]) -> List[str]:
        with self._lock:
            self._file_item_ids = set(_normalize_item_ids(item_ids))
            self._persist_locked()
            return self.list_items()

    def describe(self):
        return {
            "item_ids": self.list_items(),
            "env_item_ids": self.list_env_items(),
            "file_item_ids": self.list_file_items(),
            "file_path": self.file_path,
        }

    def _load_file_item_ids(self) -> List[str]:
        if not self.file_path or not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning(f"加载商品白名单文件失败: {exc}")
            return []

        if isinstance(payload, dict):
            item_ids = payload.get("item_ids", [])
        elif isinstance(payload, list):
            item_ids = payload
        else:
            return []
        return _normalize_item_ids(item_ids)

    def _persist_locked(self) -> None:
        if not self.file_path:
            raise ValueError("file_path is required")
        directory = os.path.dirname(self.file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as handle:
            json.dump({"item_ids": sorted(self._file_item_ids)}, handle, ensure_ascii=False, indent=2)
