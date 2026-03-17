import hashlib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Dict, List, Optional

import requests

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback for minimal test env
    import logging

    logger = logging.getLogger(__name__)

from core.handlers.base import EventHandler
from core.handlers.webhook_handler import _parse_actions, _to_bool, _to_int
from core.models import Action, Event


class OrderRouteHandler(EventHandler):
    name = "order_route"

    def __init__(
        self,
        routes: Optional[Dict[str, Dict[str, Any]]] = None,
        group_routes: Optional[Dict[str, Dict[str, Any]]] = None,
        item_id_resolver: Optional[Callable[[str], Optional[str]]] = None,
        enabled: Optional[bool] = None,
        default_timeout_ms: Optional[int] = None,
        default_retries: Optional[int] = None,
    ):
        item_routes = _normalize_item_routes(routes if routes is not None else _load_item_routes_from_env())
        grouped_item_routes = _normalize_group_routes(
            group_routes if group_routes is not None else _load_group_routes_from_env()
        )
        self.routes = dict(grouped_item_routes)
        self.routes.update(item_routes)
        self.item_id_resolver = item_id_resolver or (lambda chat_id: None)
        self.enabled = (
            _to_bool(os.getenv("ORDER_ROUTER_ENABLED", "true")) if enabled is None else bool(enabled)
        )
        self.default_timeout_ms = (
            _to_int(os.getenv("ORDER_ROUTER_TIMEOUT_MS", "3000"), 3000)
            if default_timeout_ms is None
            else int(default_timeout_ms)
        )
        self.default_retries = (
            _to_int(os.getenv("ORDER_ROUTER_RETRIES", "2"), 2)
            if default_retries is None
            else int(default_retries)
        )

    def handle(self, event: Event) -> List[Action]:
        if not self.enabled or event.event_type != "order.status.changed":
            return []

        payload = event.payload if isinstance(event.payload, dict) else {}
        chat_id = payload.get("chat_id")
        item_id = payload.get("item_id")
        if not item_id and isinstance(chat_id, str) and chat_id:
            item_id = self.item_id_resolver(chat_id)

        if not isinstance(item_id, str) or not item_id:
            logger.debug(f"order event without item_id skipped event_id={event.event_id}")
            return []

        order_status = payload.get("order_status")
        if not _should_route_order_status(order_status):
            logger.info(f"order route skipped item_id={item_id} status={order_status}")
            return []

        route = self.routes.get(item_id)
        if not route:
            logger.debug(f"order event item_id={item_id} has no route, skip")
            return []

        url = route.get("url")
        if not isinstance(url, str) or not url.strip():
            logger.warning(f"order route item_id={item_id} has invalid url")
            return []

        normalized_payload = _sanitize_json_mapping(payload)
        normalized_payload["item_id"] = item_id
        body = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "payload": normalized_payload,
            "meta": _sanitize_json_mapping(event.meta),
        }

        headers = {"Content-Type": "application/json"}
        secret = route.get("secret", "")
        if isinstance(secret, str) and secret:
            body_json = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hmac.new(secret.encode("utf-8"), body_json.encode("utf-8"), hashlib.sha256).hexdigest()
            headers["X-Agent-Signature"] = f"sha256={digest}"

        retries = _to_int(str(route.get("retries", self.default_retries)), self.default_retries)
        timeout_ms = _to_int(str(route.get("timeout_ms", self.default_timeout_ms)), self.default_timeout_ms)

        attempts = max(retries, 0) + 1
        timeout = max(timeout_ms, 1) / 1000.0
        logger.info(
            f"order route matched item_id={item_id} status={normalized_payload.get('order_status')} "
            f"url={url.strip()} attempts={attempts}"
        )
        for attempt in range(attempts):
            try:
                response = requests.post(url.strip(), json=body, headers=headers, timeout=timeout)
            except Exception as exc:
                logger.warning(
                    f"order route call failed item_id={item_id} attempt={attempt + 1}/{attempts} err={exc}"
                )
                continue

            if 200 <= response.status_code < 300:
                actions = _parse_actions(response)
                logger.info(
                    f"order route delivered item_id={item_id} status_code={response.status_code} actions={len(actions)}"
                )
                return actions

            logger.warning(
                f"order route non-2xx item_id={item_id} attempt={attempt + 1}/{attempts} status={response.status_code}"
            )

        return []


def _should_route_order_status(order_status: Any) -> bool:
    if not isinstance(order_status, str):
        return False

    normalized = order_status.strip()
    if not normalized:
        return False

    blocked_keywords = (
        "退款",
        "待付款",
        "未付款",
        "关闭订单",
        "交易关闭",
        "关闭了订单",
        "已关闭",
        "取消",
    )
    if any(keyword in normalized for keyword in blocked_keywords):
        return False

    allowed_keywords = (
        "已付款",
        "等待卖家发货",
        "等待你发货",
        "待发货",
        "买家已付款",
    )
    return any(keyword in normalized for keyword in allowed_keywords)


_SKIP_JSON = object()


def _sanitize_json_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    sanitized: Dict[str, Any] = {}
    for key, item in value.items():
        if key == "websocket":
            continue
        if not isinstance(key, str):
            continue
        normalized = _sanitize_json_value(item)
        if normalized is _SKIP_JSON:
            continue
        sanitized[key] = normalized
    return sanitized


def _sanitize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return _sanitize_json_mapping(value)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized_items: List[Any] = []
        for item in value:
            normalized = _sanitize_json_value(item)
            if normalized is _SKIP_JSON:
                continue
            normalized_items.append(normalized)
        return normalized_items

    return _SKIP_JSON


def _load_item_routes_from_env() -> Dict[str, Dict[str, Any]]:
    raw = os.getenv("ORDER_ITEM_WEBHOOK_ROUTES", "{}").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.warning(f"invalid ORDER_ITEM_WEBHOOK_ROUTES config: {exc}")
    return {}


def _load_group_routes_from_env() -> Dict[str, Dict[str, Any]]:
    raw = os.getenv("ORDER_GROUP_WEBHOOK_ROUTES", "{}").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.warning(f"invalid ORDER_GROUP_WEBHOOK_ROUTES config: {exc}")
    return {}


def _normalize_item_routes(routes: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    for item_id, config in routes.items():
        if not isinstance(item_id, str) or not item_id:
            continue

        if isinstance(config, str):
            normalized[item_id] = {"url": config.strip()}
            continue

        if not isinstance(config, dict):
            continue

        route = dict(config)
        if "url" not in route and isinstance(route.get("webhook_url"), str):
            route["url"] = route["webhook_url"]
        normalized[item_id] = route
    return normalized


def _normalize_group_routes(group_routes: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    for group_name, config in group_routes.items():
        if not isinstance(config, dict):
            continue
        items = config.get("items")
        if not isinstance(items, list):
            logger.warning(f"order group={group_name} missing items list, skipped")
            continue

        route = dict(config)
        route.pop("items", None)
        if "url" not in route and isinstance(route.get("webhook_url"), str):
            route["url"] = route["webhook_url"]

        for item_id in items:
            if not isinstance(item_id, str) or not item_id:
                continue
            normalized[item_id] = route
    return normalized
