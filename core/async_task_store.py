import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback for minimal test env
    import logging

    logger = logging.getLogger(__name__)


class AsyncTaskStore:
    def __init__(self, db_path: str = "data/chat_history.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS async_tasks (
                task_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                to_user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                status TEXT NOT NULL,
                status_url TEXT NOT NULL,
                status_method TEXT NOT NULL DEFAULT 'GET',
                status_headers TEXT NOT NULL DEFAULT '{}',
                status_body TEXT NOT NULL DEFAULT '{}',
                poll_interval_seconds INTEGER NOT NULL DEFAULT 5,
                next_poll_at INTEGER NOT NULL,
                last_polled_at INTEGER,
                last_error TEXT,
                last_response TEXT,
                last_notified_status TEXT,
                completed_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_async_tasks_next_poll_at
            ON async_tasks(next_poll_at)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_async_tasks_completed_at
            ON async_tasks(completed_at)
            """
        )
        conn.commit()
        conn.close()

    def upsert_task(self, payload: Dict[str, Any]) -> bool:
        normalized = _normalize_task_payload(payload)
        if normalized is None:
            logger.warning(f"invalid async task payload skipped: {payload}")
            return False

        now_ts = int(time.time())
        record = {
            "task_id": normalized["task_id"],
            "chat_id": normalized["chat_id"],
            "to_user_id": normalized["to_user_id"],
            "item_id": normalized["item_id"],
            "status": normalized["status"],
            "status_url": normalized["status_url"],
            "status_method": normalized["status_method"],
            "status_headers": json.dumps(normalized["status_headers"], ensure_ascii=False, sort_keys=True),
            "status_body": json.dumps(normalized["status_body"], ensure_ascii=False, sort_keys=True),
            "poll_interval_seconds": normalized["poll_interval_seconds"],
            "next_poll_at": normalized["next_poll_at"],
            "last_notified_status": normalized["last_notified_status"],
            "created_at": now_ts,
            "updated_at": now_ts,
        }

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO async_tasks (
                    task_id, chat_id, to_user_id, item_id, status, status_url, status_method,
                    status_headers, status_body, poll_interval_seconds, next_poll_at,
                    last_notified_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    to_user_id = excluded.to_user_id,
                    item_id = excluded.item_id,
                    status = excluded.status,
                    status_url = excluded.status_url,
                    status_method = excluded.status_method,
                    status_headers = excluded.status_headers,
                    status_body = excluded.status_body,
                    poll_interval_seconds = excluded.poll_interval_seconds,
                    next_poll_at = excluded.next_poll_at,
                    last_notified_status = COALESCE(excluded.last_notified_status, async_tasks.last_notified_status),
                    updated_at = excluded.updated_at,
                    completed_at = NULL
                """,
                (
                    record["task_id"],
                    record["chat_id"],
                    record["to_user_id"],
                    record["item_id"],
                    record["status"],
                    record["status_url"],
                    record["status_method"],
                    record["status_headers"],
                    record["status_body"],
                    record["poll_interval_seconds"],
                    record["next_poll_at"],
                    record["last_notified_status"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.warning(f"async task upsert failed task_id={record['task_id']} err={exc}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def list_due_tasks(self, now_ts: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        current = int(now_ts if now_ts is not None else time.time())
        batch_size = max(int(limit), 1)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM async_tasks
                WHERE completed_at IS NULL AND next_poll_at <= ?
                ORDER BY next_poll_at ASC, created_at ASC
                LIMIT ?
                """,
                (current, batch_size),
            )
            rows = cursor.fetchall()
            return [_row_to_task(row) for row in rows]
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(task_id, str) or not task_id:
            return None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM async_tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            return _row_to_task(row) if row else None
        finally:
            conn.close()

    def update_task(self, task_id: str, **fields: Any) -> bool:
        if not isinstance(task_id, str) or not task_id:
            return False

        allowed = {
            "status",
            "next_poll_at",
            "last_polled_at",
            "last_error",
            "last_response",
            "last_notified_status",
            "completed_at",
            "status_url",
            "status_method",
            "status_headers",
            "status_body",
            "poll_interval_seconds",
        }
        updates = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"status_headers", "status_body"} and isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            updates.append(f"{key} = ?")
            values.append(value)

        updates.append("updated_at = ?")
        values.append(int(time.time()))
        values.append(task_id)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE async_tasks SET {', '.join(updates)} WHERE task_id = ?",
                tuple(values),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception as exc:
            logger.warning(f"async task update failed task_id={task_id} err={exc}")
            conn.rollback()
            return False
        finally:
            conn.close()


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    if row is None:
        return {}

    task = dict(row)
    task["status_headers"] = _load_json_object(task.get("status_headers"))
    task["status_body"] = _load_json_object(task.get("status_body"))
    return task


def _load_json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _normalize_task_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    task_id = _require_text(payload.get("task_id"))
    chat_id = _require_text(payload.get("chat_id"))
    to_user_id = _require_text(payload.get("to_user_id"))
    item_id = _require_text(payload.get("item_id"))
    status_url = _normalize_url(payload.get("status_url"))
    if not all([task_id, chat_id, to_user_id, item_id, status_url]):
        return None

    status = _normalize_status(payload.get("status") or "await_login")
    poll_interval_seconds = _to_positive_int(payload.get("poll_interval_seconds"), default=5)
    next_poll_at = _to_positive_int(payload.get("next_poll_at"), default=int(time.time()))
    status_method = str(payload.get("status_method") or "GET").strip().upper()
    if status_method not in {"GET", "POST"}:
        status_method = "GET"

    status_headers = payload.get("status_headers")
    if not isinstance(status_headers, dict):
        status_headers = {}

    status_body = payload.get("status_body")
    if not isinstance(status_body, dict):
        status_body = {}

    last_notified_status = payload.get("last_notified_status", status)
    if isinstance(last_notified_status, str) and last_notified_status:
        last_notified_status = _normalize_status(last_notified_status)
    elif last_notified_status is None:
        last_notified_status = None
    else:
        last_notified_status = status

    return {
        "task_id": task_id,
        "chat_id": chat_id,
        "to_user_id": to_user_id,
        "item_id": item_id,
        "status": status,
        "status_url": status_url,
        "status_method": status_method,
        "status_headers": status_headers,
        "status_body": status_body,
        "poll_interval_seconds": poll_interval_seconds,
        "next_poll_at": next_poll_at,
        "last_notified_status": last_notified_status,
    }


def _require_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_url(value: Any) -> Optional[str]:
    text = _require_text(value)
    if not text:
        return None
    if not (text.startswith("http://") or text.startswith("https://")):
        return None
    return text


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
