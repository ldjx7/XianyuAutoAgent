# Xianyu Workflow Integration Contract

本文档约定 `XianyuAutoAgent` 与外部异步业务系统之间的标准对接方式。

适用场景：
- 买家下单后，需要由外部系统启动异步处理。
- 外部系统需要先返回二维码图片、登录链接等中间结果。
- 外部系统处理完成后，需要由 Agent 通知买家，必要时由 AI 结合上下文渲染最终话术。

## 一、推荐架构

不要让 Agent 直接调用业务系统现有的内部接口，例如：
- `/api/auth/bili/qr/start`
- `/api/auth/bili/qr/poll`

原因：
- Agent 订单路由入口要求响应格式为 `{"actions":[...]}`。
- Agent 异步轮询入口要求响应顶层包含 `status`、`message`、`result`、`presentation` 等字段。
- 现有业务接口通常返回 `{"data": ...}`，需要做一层协议转换。

推荐做法是由外部系统新增一层很薄的“Xianyu 适配接口”：

1. `POST /api/integration/xianyu/order-start`
2. `GET /api/integration/xianyu/tasks/{task_id}`

外部系统内部再去调用你已有的业务接口。

## 二、职责边界

### Agent 负责

- 接收闲鱼消息和订单事件。
- 根据 `ORDER_ITEM_WEBHOOK_ROUTES` 把命中的订单事件转发给外部系统。
- 执行外部系统返回的标准动作：
  - `send_text`
  - `send_image`
  - `set_manual_mode`
  - `track_async_task`
  - `render_message`
- 轮询异步任务状态。
- 对 `presentation.mode=ai` 的结果使用当前聊天上下文、商品信息和 AI 模型渲染最终话术。

### 外部系统负责

- 决定何时开始业务处理。
- 产出二维码图片地址、备用登录链接、任务标识。
- 保存 `task_id` 与内部业务实体之间的映射。
- 提供给 Agent 使用的开始接口和轮询接口。
- 给出真实业务事实；不要把关键业务判断交给 Agent。

## 三、事件入口

当闲鱼订单事件命中某个商品路由时，Agent 会向你配置的 URL 发起 `POST` 请求。

请求体格式：

```json
{
  "event_id": "order.status.changed:xxxx",
  "event_type": "order.status.changed",
  "occurred_at": 1773211111222,
  "payload": {
    "chat_id": "59255969245",
    "user_id": "2222127989978",
    "item_id": "939187923532",
    "order_status": "买家已付款",
    "raw": {}
  },
  "meta": {}
}
```

说明：
- `item_id` 可能来自聊天上下文回填。
- `order_status` 是当前闲鱼订单事件文本，不保证完全标准化。
- 外部系统应自行判断哪些订单状态才需要触发业务处理。

如果当前订单状态不应触发处理，返回：

```json
{
  "actions": []
}
```

## 四、开始处理接口规约

### 4.1 推荐入口

`POST /api/integration/xianyu/order-start`

该接口是给 Agent 调用的适配层接口，不建议直接暴露内部业务接口给 Agent。

### 4.2 处理逻辑

1. 校验 `X-Agent-Signature`。
2. 校验 `event_type == order.status.changed`。
3. 根据 `payload.order_status` 判断是否应启动处理。
4. 内部调用业务系统的 `/api/auth/bili/qr/start`。
5. 将业务返回转换为 Agent 标准动作。
6. 返回 `{"actions":[...]}`。

### 4.3 与现有业务接口的映射

业务系统原始返回：

```json
{
  "data": {
    "expires_at": "2026-03-11T09:31:40.907839Z",
    "qr_image_url": "http://localhost:8081/api/public/bili/qr/image?token=...",
    "qr_key": "e96c0697acef0d43099df7f2ccb3c761",
    "qr_status_url": "http://localhost:8081/api/public/bili/qr/status?token=...",
    "qr_url": "https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/auth?auth_code=e96c0697acef0d43099df7f2ccb3c761",
    "status_url": "http://localhost:8081/api/public/bili/qr/status?token=..."
  }
}
```

推荐转换后返回：

```json
{
  "actions": [
    {
      "action_type": "send_image",
      "payload": {
        "chat_id": "59255969245",
        "to_user_id": "2222127989978",
        "image_url": "http://bili-service:8081/api/public/bili/qr/image?token=...",
        "text": "请扫码完成 B 站登录，登录后系统会自动开始答题。",
        "fallback_text": "如果图片未展示，请打开备用链接登录："
      }
    },
    {
      "action_type": "send_text",
      "payload": {
        "chat_id": "59255969245",
        "to_user_id": "2222127989978",
        "text": "备用登录链接： https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/auth?auth_code=e96c0697acef0d43099df7f2ccb3c761"
      }
    },
    {
      "action_type": "track_async_task",
      "payload": {
        "task_id": "bili_exam_e96c0697acef0d43099df7f2ccb3c761",
        "chat_id": "59255969245",
        "to_user_id": "2222127989978",
        "item_id": "939187923532",
        "status": "await_login",
        "status_url": "http://bili-service:8081/api/integration/xianyu/tasks/bili_exam_e96c0697acef0d43099df7f2ccb3c761",
        "poll_interval_seconds": 5
      }
    }
  ]
}
```

### 4.4 重要要求

- `qr_image_url` 如果返回 `image/png` 二进制流，可以直接作为 `send_image.payload.image_url`。
- Docker 网络中不要使用 `localhost`。请替换为容器服务名，例如 `http://bili-service:8081/...`。
- 推荐 `task_id` 由适配层生成并持久化，用于关联：
  - `chat_id`
  - `user_id`
  - `item_id`
  - `qr_key`
  - 内部 job id
  - 轮询所需上下文

## 五、轮询接口规约

### 5.1 推荐入口

`GET /api/integration/xianyu/tasks/{task_id}`

该接口由 Agent 异步轮询器调用。

### 5.2 处理逻辑

1. 根据 `task_id` 查到内部任务上下文。
2. 内部调用业务系统的 `/api/auth/bili/qr/poll`。
3. 将 `{"data": ...}` 展平为 Agent 可识别的顶层结构。
4. 返回状态、消息、结果，以及可选的 `presentation`。

### 5.3 与现有业务接口的映射

业务系统原始返回：

```json
{
  "data": {
    "auto_job_id": "1a2191df-07f4-4997-b498-f02dfeee05ff",
    "job": {
      "id": "1a2191df-07f4-4997-b498-f02dfeee05ff",
      "status": "failed",
      "message": "答题结束，未通过。本次得分 0/100 分。",
      "score": 0,
      "question_idx": 1,
      "total_questions": 100,
      "interaction_count": 1
    },
    "message": "答题结束，未通过。本次得分 0/100 分。",
    "mid": "38436079",
    "qr_status": "confirmed",
    "status": "failed"
  }
}
```

推荐转换后返回：

```json
{
  "status": "failed",
  "message": "答题结束，未通过。本次得分 0/100 分。",
  "result": {
    "auto_job_id": "1a2191df-07f4-4997-b498-f02dfeee05ff",
    "job_id": "1a2191df-07f4-4997-b498-f02dfeee05ff",
    "score": 0,
    "question_idx": 1,
    "total_questions": 100,
    "interaction_count": 1,
    "qr_status": "confirmed",
    "mid": "38436079"
  },
  "presentation": {
    "mode": "ai",
    "scene": "workflow_result",
    "instructions": {
      "tone": "自然、简洁、像闲鱼卖家",
      "must_include": ["未通过", "0/100 分"]
    },
    "fallback_text": "答题结束，未通过。本次得分 0/100 分。"
  }
}
```

## 六、状态映射建议

建议外部系统统一输出以下状态之一：

| 外部状态 | 含义 | Agent 行为 |
| --- | --- | --- |
| `await_login` | 二维码已生成，等待扫码/确认 | 首次通知买家，等待后续轮询 |
| `running` | 已登录，正在答题或处理中 | 状态变化时可通知一次 |
| `passed` | 处理成功 | 终态，Agent 停止轮询 |
| `failed` | 处理失败 | 终态，Agent 停止轮询 |
| `timeout` | 二维码过期或超时 | 终态，Agent 停止轮询 |
| `cancelled` | 任务被取消 | 终态，Agent 停止轮询 |

建议：
- Agent 只在状态变化时通知一次。
- 不建议用 `question_idx` 等细粒度进度频繁刷消息。
- 如果确实需要主动推送阶段性文案，请让轮询结果在关键节点变更 `status`，或直接返回 `actions`。

## 七、AI 渲染结果规约

如果你希望最终发给买家的消息由 Agent 结合上下文和商品信息润色，请返回：

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
      "tone": "自然、简洁、像闲鱼卖家",
      "must_include": ["答题已完成", "96 分"]
    },
    "fallback_text": "答题已完成，成绩 96 分。"
  }
}
```

边界约定：
- 外部系统负责“事实是什么”。
- Agent 负责“怎么说更自然”。
- 分数、是否通过、下一步操作等关键事实必须由外部系统提供，不要让 Agent 推断。

## 八、图片发送规约

`send_image.payload.image_url` 推荐直接指向返回图片二进制流的接口。

要求：
- `Content-Type` 必须是 `image/png`、`image/jpeg`、`image/gif`、`image/webp` 之一。
- 接口响应体应直接是图片字节，不要返回 HTML、JSON 或 base64 包装。
- 图片地址可以是短时 token 化的公开地址。

Agent 当前会：
1. 拉取 `image_url`。
2. 校验其确实返回图片。
3. 上传到闲鱼图片消息接口。
4. 发送原生图片消息。
5. 若 `text` 非空，再补发一条说明文本。

## 九、已读与消息展示

Agent 当前在收到买家消息后，会尝试：
- 标记会话已读
- 清理会话未读红点

因此在正确部署下，买家消息不应长期停留在“未读”状态。

如果仍出现未读残留，请优先检查：
- 当前容器是否已部署到包含已读逻辑的新版本
- websocket 是否在处理消息后立即断开
- 闲鱼接口是否返回了非 200

## 十、配置示例

`.env` 中按商品配置路由：

```env
ORDER_ROUTER_ENABLED=true
ORDER_ITEM_WEBHOOK_ROUTES={"939187923532":{"url":"http://bili-service:8081/api/integration/xianyu/order-start","secret":"replace_me","timeout_ms":5000,"retries":2}}
ORDER_GROUP_WEBHOOK_ROUTES={}
```

## 十一、实现建议

推荐外部系统按以下方式落地：

1. 新增适配层接口，不直接暴露内部业务接口给 Agent。
2. 在适配层保存 `task_id -> 内部业务上下文`。
3. 轮询接口保持幂等，相同任务的同一状态可重复返回。
4. 所有内部 URL 在 Docker 网络里都使用服务名，不用 `localhost`。
5. 优先让外部系统返回事实，Agent 用 AI 只负责表达。

## 十二、相关代码与文档

- 动作定义：[docs/actions.md](../actions.md)
- webhook 示例：[docs/integration/webhook-example.md](./webhook-example.md)
- 订单路由处理：[core/handlers/order_route_handler.py](../../core/handlers/order_route_handler.py)
- 异步轮询：[core/async_task_poller.py](../../core/async_task_poller.py)
- 动作执行：[core/action_executor.py](../../core/action_executor.py)
