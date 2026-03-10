# Webhook Integration Example

## 开启方式

```bash
EVENT_HANDLERS=core.handlers.webhook_handler.WebhookHandler
EVENT_WEBHOOK_ENABLED=true
EVENT_WEBHOOK_URL=http://127.0.0.1:8000/events
EVENT_WEBHOOK_TIMEOUT_MS=3000
EVENT_WEBHOOK_RETRIES=2
EVENT_WEBHOOK_SECRET=replace_me

# 订单按商品路由到不同服务（可选）
ORDER_ROUTER_ENABLED=true
ORDER_ITEM_WEBHOOK_ROUTES={"itemA":{"url":"http://127.0.0.1:8000/events/a","secret":"sa"},"itemB":{"url":"http://127.0.0.1:8000/events/b","secret":"sb"}}
ORDER_GROUP_WEBHOOK_ROUTES={"ship_group":{"items":["itemC","itemD"],"url":"http://127.0.0.1:8000/events/group","secret":"sg"}}
ORDER_ROUTER_TIMEOUT_MS=3000
ORDER_ROUTER_RETRIES=2
```

## 签名说明

- Header: `X-Agent-Signature`
- 算法: `HMAC-SHA256`
- 取值格式: `sha256=<hex_digest>`
- 签名原文: 事件 JSON（`sort_keys=True` + 紧凑序列化）

## 参考服务（Python）

```python
import hashlib
import hmac
import json
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = "replace_me"


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.split("=", 1)[1], expected)


@app.post("/events")
def on_event():
    signature = request.headers.get("X-Agent-Signature", "")
    raw = request.get_data()
    if not verify_signature(raw, signature):
        return jsonify({"error": "bad signature"}), 401

    event = request.get_json(force=True)
    if event.get("event_type") == "order.status.changed":
        return jsonify(
            {
                "actions": [
                    {
                        "action_type": "send_text",
                        "payload": {
                            "chat_id": event["payload"].get("chat_id"),
                            "to_user_id": event["payload"].get("user_id"),
                            "text": "订单状态已更新，稍后为您处理。",
                        },
                    },
                    {
                        "action_type": "track_async_task",
                        "payload": {
                            "task_id": "task_abc",
                            "chat_id": event["payload"].get("chat_id"),
                            "to_user_id": event["payload"].get("user_id"),
                            "item_id": event["payload"].get("item_id"),
                            "status": "await_login",
                            "status_url": "http://127.0.0.1:8000/tasks/task_abc",
                            "poll_interval_seconds": 5,
                        },
                    },
                ]
            }
        )

    return jsonify({"actions": []})
```

该示例只基于事件契约做决策，不依赖特定业务流程。

## 轮询状态接口（示例）

`track_async_task.payload.status_url` 指向的接口建议返回：

```json
{
  "status": "passed",
  "message": "已完成考试，成绩 96 分。"
}
```

也可以直接返回动作列表：

```json
{
  "status": "running",
  "actions": [
    {
      "action_type": "send_text",
      "payload": {
        "chat_id": "chat123",
        "to_user_id": "user123",
        "text": "已检测到登录，正在开始答题。"
      }
    }
  ]
}
```

建议：
- 用 `ORDER_ITEM_WEBHOOK_ROUTES` 只给需要异步履约的商品开启这条链路。
- 轮询状态接口保持幂等，同一 `task_id` 多次查询返回同一状态。
