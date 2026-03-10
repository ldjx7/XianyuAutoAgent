import json
import os
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback for minimal test env
    import logging

    logger = logging.getLogger(__name__)

from core.handlers.webhook_handler import _parse_actions
from core.models import Action


FINAL_STATUSES = {"passed", "failed", "timeout", "cancelled", "completed"}


class AsyncTaskPoller:
    def __init__(self, store, timeout_ms: Optional[int] = None, batch_size: Optional[int] = None):
        self.store = store
        self.timeout_ms = _to_positive_int(
            timeout_ms if timeout_ms is not None else os.getenv("ASYNC_TASK_POLL_TIMEOUT_MS", "3000"),
            default=3000,
        )
        self.batch_size = _to_positive_int(
            batch_size if batch_size is not None else os.getenv("ASYNC_TASK_POLL_BATCH_SIZE", "20"),
            default=20,
        )

    def poll_due_tasks(self, now_ts: Optional[int] = None) -> List[Action]:
        current = int(now_ts if now_ts is not None else time.time())
        tasks = self.store.list_due_tasks(now_ts=current, limit=self.batch_size)
        actions: List[Action] = []
        for task in tasks:
            actions.extend(self._poll_task(task, current))
        return actions

    def _poll_task(self, task: Dict[str, Any], now_ts: int) -> List[Action]:
        task_id = task["task_id"]
        interval = _to_positive_int(task.get("poll_interval_seconds"), default=5)
        next_poll_at = now_ts + interval
        try:
            response = self._request_status(task)
        except Exception as exc:
            logger.warning(f"async task poll failed task_id={task_id} err={exc}")
            self.store.update_task(
                task_id,
                next_poll_at=next_poll_at,
                last_polled_at=now_ts,
                last_error=str(exc),
            )
            return []

        if not (200 <= response.status_code < 300):
            self.store.update_task(
                task_id,
                next_poll_at=next_poll_at,
                last_polled_at=now_ts,
                last_error=f"http {response.status_code}",
            )
            return []

        try:
            data = response.json()
        except Exception as exc:
            self.store.update_task(
                task_id,
                next_poll_at=next_poll_at,
                last_polled_at=now_ts,
                last_error=f"invalid json: {exc}",
            )
            return []

        if not isinstance(data, dict):
            self.store.update_task(
                task_id,
                next_poll_at=next_poll_at,
                last_polled_at=now_ts,
                last_error="invalid response payload",
            )
            return []

        status = _normalize_status(data.get("status") or data.get("task_status") or task.get("status"))
        serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
        should_notify = status != task.get("last_notified_status")
        actions = []
        if should_notify:
            actions = self._extract_actions(data, task, status)

        completed_at = now_ts if status in FINAL_STATUSES else None
        self.store.update_task(
            task_id,
            status=status,
            next_poll_at=next_poll_at,
            last_polled_at=now_ts,
            last_error=None,
            last_response=serialized,
            last_notified_status=status if actions else task.get("last_notified_status"),
            completed_at=completed_at,
        )
        return actions

    def _request_status(self, task: Dict[str, Any]):
        method = str(task.get("status_method") or "GET").upper()
        url = task.get("status_url")
        headers = task.get("status_headers") if isinstance(task.get("status_headers"), dict) else {}
        body = task.get("status_body") if isinstance(task.get("status_body"), dict) else {}
        timeout = max(self.timeout_ms, 1) / 1000.0

        kwargs = {"headers": headers, "timeout": timeout}
        if method == "GET":
            if body:
                kwargs["params"] = body
        else:
            kwargs["json"] = body

        return requests.request(method, url, **kwargs)

    def _extract_actions(self, data: Dict[str, Any], task: Dict[str, Any], status: str) -> List[Action]:
        parsed = _parse_actions(SimpleNamespace(json=lambda: data))
        if parsed:
            return parsed

        message = _extract_message(data)
        if not message:
            return []

        return [
            Action(
                action_type="send_text",
                payload={
                    "chat_id": task["chat_id"],
                    "to_user_id": task["to_user_id"],
                    "text": message,
                },
                meta={"task_id": task["task_id"], "status": status},
            )
        ]


def _extract_message(data: Dict[str, Any]) -> Optional[str]:
    for key in ("message", "notify_text", "result_message", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        return "await_login"
    text = value.strip().lower()
    return text or "await_login"


def _to_positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
        return number if number > 0 else default
    except Exception:
        return default
