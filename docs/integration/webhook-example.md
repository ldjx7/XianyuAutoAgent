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
                        "action_type": "send_image",
                        "payload": {
                            "chat_id": event["payload"].get("chat_id"),
                            "to_user_id": event["payload"].get("user_id"),
                            "image_url": "http://127.0.0.1:8080/api/public/bili/qr/image?token=example",
                            "text": "请扫码完成 B 站登录",
                            "fallback_text": "如果图片未展示，请打开二维码链接："
                        }
                    },
                    {
                        "action_type": "send_text",
                        "payload": {
                            "chat_id": event["payload"].get("chat_id"),
                            "to_user_id": event["payload"].get("user_id"),
                            "text": "二维码已发送，登录后系统会自动开始处理。",
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

## 使用 AI 渲染业务结果（可选）

如果你希望订单系统只返回结构化结果，由 Agent 结合当前聊天上下文和商品信息生成更自然的话术，可以返回 `render_message`：

```json
{
  "actions": [
    {
      "action_type": "render_message",
      "payload": {
        "chat_id": "chat123",
        "to_user_id": "user123",
        "scene": "workflow_result",
        "facts": {
          "status": "passed",
          "message": "答题已完成，成绩 96 分。",
          "result": {
            "score": 96,
            "exam_name": "B站课程测试"
          }
        },
        "instructions": {
          "tone": "自然、简洁、像闲鱼卖家",
          "must_include": ["考试已完成", "96 分"]
        },
        "fallback_text": "答题已完成，成绩 96 分。"
      }
    }
  ]
}
```

建议边界：
- 业务系统负责给出真实业务事实。
- Agent 负责把这些事实渲染成适合闲鱼对话场景的话术。
- 如果你不希望 AI 改写话术，就继续返回 `send_text`。

## 轮询状态接口（示例）

`track_async_task.payload.status_url` 指向的接口建议返回：

```json
{
  "status": "passed",
  "message": "已完成考试，成绩 96 分。"
}
```

如果你希望轮询结果自动走 AI 渲染，可以返回：

```json
{
  "status": "passed",
  "message": "答题已完成，成绩 96 分。",
  "result": {
    "score": 96,
    "exam_name": "B站课程测试"
  },
  "presentation": {
    "mode": "ai",
    "scene": "workflow_result",
    "instructions": {
      "tone": "自然、简洁、像闲鱼卖家"
    },
    "fallback_text": "答题已完成，成绩 96 分。"
  }
}
```

在这种情况下，Agent 会自动把轮询结果桥接为 `render_message` 动作。

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
- 对图片类登录流程，推荐同时返回 `send_image` 和 `track_async_task`；必要时再补一条文本兜底。
