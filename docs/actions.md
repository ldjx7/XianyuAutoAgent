# Action Schema

处理器返回动作列表，执行器按顺序执行：

```json
[
  {
    "action_type": "string",
    "payload": {},
    "meta": {}
  }
]
```

## Built-in Action Types

### 1. `send_text`
向目标会话发送文本消息。

```json
{
  "action_type": "send_text",
  "payload": {
    "chat_id": "chat123",
    "to_user_id": "user123",
    "text": "您好，库存充足。"
  },
  "meta": {}
}
```

### 2. `send_image`
向目标会话发送图片消息。

```json
{
  "action_type": "send_image",
  "payload": {
    "chat_id": "chat123",
    "to_user_id": "user123",
    "image_url": "https://biz.example.com/api/public/bili/qr/image?token=...",
    "text": "请使用闲鱼内置浏览器或另一台设备扫码登录",
    "fallback_text": "二维码图片如下，如果图片未正常展示请打开该链接："
  },
  "meta": {}
}
```

约定：
- `chat_id`、`to_user_id`、`image_url` 为必填。
- `text` 为图片说明文案，可选。
- 若执行器未配置原生图片发送能力，会自动退化为发送 `fallback_text + image_url` 的文本消息。

### 3. `set_manual_mode`
切换会话人工接管模式。

```json
{
  "action_type": "set_manual_mode",
  "payload": {
    "chat_id": "chat123",
    "enabled": true
  },
  "meta": {}
}
```

### 4. `track_async_task`
记录一个需要后台轮询的外部异步任务。

```json
{
  "action_type": "track_async_task",
  "payload": {
    "task_id": "task_abc",
    "chat_id": "chat123",
    "to_user_id": "user123",
    "item_id": "item123",
    "status": "await_login",
    "status_url": "https://biz.example.com/tasks/task_abc",
    "poll_interval_seconds": 5,
    "status_method": "GET",
    "status_headers": {
      "Authorization": "Bearer token"
    },
    "status_body": {}
  },
  "meta": {}
}
```

约定：
- `task_id`、`chat_id`、`to_user_id`、`item_id`、`status_url` 为必填。
- `status` 默认可用值包括：`await_login`、`running`、`passed`、`failed`、`timeout`。
- `status_method` 目前支持 `GET` / `POST`。
- 轮询接口返回 `status` 变化时，如果响应里带 `message` 或 `actions`，Agent 会通知一次并更新任务状态。

未知 `action_type` 会被安全忽略并记录 warning。
