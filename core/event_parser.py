import hashlib
import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from core.models import Event


def parse_events(message_data: Dict[str, Any]) -> List[Event]:
    if not isinstance(message_data, dict):
        return []

    events: List[Event] = []
    order_status = _extract_order_status(message_data)
    if order_status:
        payload = {
            "chat_id": _extract_order_chat_id(message_data),
            "user_id": _extract_order_user_id(message_data),
            "item_id": None,
            "order_status": order_status,
            "raw": message_data,
        }
        events.append(
            Event(
                event_id=_build_event_id("order.status.changed", message_data),
                event_type="order.status.changed",
                occurred_at=int(time.time() * 1000),
                payload=payload,
            )
        )

    if _is_chat_message(message_data):
        message_node = message_data["1"]
        content_node = message_node["10"]
        reminder_url = content_node.get("reminderUrl", "")
        occurred_at = _parse_int(message_node.get("5"), default=int(time.time() * 1000))
        payload = {
            "chat_id": _normalize_id(message_node.get("2")),
            "user_id": _normalize_id(content_node.get("senderUserId")),
            "item_id": _extract_item_id(reminder_url),
            "order_status": _extract_order_status(message_data),
            "message": content_node.get("reminderContent"),
            "sender_name": content_node.get("reminderTitle"),
            "created_at": occurred_at,
            "raw": message_data,
        }
        events.append(
            Event(
                event_id=_build_event_id("chat.message.received", message_data),
                event_type="chat.message.received",
                occurred_at=occurred_at,
                payload=payload,
            )
        )

    return events


def _is_chat_message(message: Dict[str, Any]) -> bool:
    node = message.get("1")
    if not isinstance(node, dict):
        return False
    content = node.get("10")
    return isinstance(content, dict) and "reminderContent" in content


def _extract_order_status(message: Dict[str, Any]) -> Optional[str]:
    node = message.get("3")
    if not isinstance(node, dict):
        return _extract_order_status_from_chat_payload(message)
    reminder = node.get("redReminder")
    if isinstance(reminder, str) and reminder:
        return reminder
    return _extract_order_status_from_chat_payload(message)


def _extract_order_status_from_chat_payload(message: Dict[str, Any]) -> Optional[str]:
    node = message.get("1")
    if not isinstance(node, dict):
        return None
    content = node.get("10")
    if not isinstance(content, dict):
        return None

    reminder_content = content.get("reminderContent")
    reminder_title = content.get("reminderTitle")
    if not _looks_like_order_status(reminder_content, reminder_title):
        return None

    normalized_content = _normalize_bracket_message(reminder_content)
    if normalized_content:
        return normalized_content

    if isinstance(reminder_title, str) and reminder_title.strip():
        return reminder_title.strip()
    return None


def _looks_like_order_status(reminder_content: Any, reminder_title: Any) -> bool:
    content_text = reminder_content.strip() if isinstance(reminder_content, str) else ""
    title_text = reminder_title.strip() if isinstance(reminder_title, str) else ""
    combined = f"{title_text} {content_text}"
    if not combined.strip():
        return False

    keywords = (
        "待付款",
        "已付款",
        "等待你发货",
        "等待卖家发货",
        "卖家已发货",
        "买家已拍下",
        "我已拍下",
        "我已付款",
        "待收货",
        "确认收货",
        "交易关闭",
        "退款",
    )
    return any(keyword in combined for keyword in keywords)


def _normalize_bracket_message(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("[") and text.endswith("]") and len(text) >= 2:
        text = text[1:-1].strip()
    return text or None


def _extract_item_id(reminder_url: str) -> Optional[str]:
    if not isinstance(reminder_url, str) or not reminder_url:
        return None
    try:
        parsed = urlparse(reminder_url)
        query = parse_qs(parsed.query)
        item_values = query.get("itemId")
        if item_values:
            return item_values[0]
    except Exception:
        return None
    return None


def _extract_order_chat_id(message: Dict[str, Any]) -> Optional[str]:
    chat_id = _normalize_id(message.get("2"))
    if chat_id:
        return chat_id

    node = message.get("1")
    if not isinstance(node, dict):
        return None
    return _normalize_id(node.get("2"))


def _extract_order_user_id(message: Dict[str, Any]) -> Optional[str]:
    user_id = _normalize_id(message.get("1"))
    if user_id:
        return user_id

    node = message.get("1")
    if not isinstance(node, dict):
        return None
    content = node.get("10")
    if not isinstance(content, dict):
        return None
    return _normalize_id(content.get("senderUserId"))


def _normalize_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("@goofish"):
        return value.split("@", 1)[0]
    return value


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _build_event_id(event_type: str, payload: Dict[str, Any]) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{event_type}:{normalized}".encode("utf-8")).hexdigest()[:24]
    return f"{event_type}:{digest}"
