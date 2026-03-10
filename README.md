# 🚀 Xianyu AutoAgent - 智能闲鱼客服机器人系统

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/) [![LLM Powered](https://img.shields.io/badge/LLM-powered-FF6F61)](https://platform.openai.com/)

专为闲鱼平台打造的AI值守解决方案，实现闲鱼平台7×24小时自动化值守，支持多专家协同决策、智能议价和上下文感知对话。 


## 🌟 核心特性

### 智能对话引擎
| 功能模块   | 技术实现            | 关键特性                                                     |
| ---------- | ------------------- | ------------------------------------------------------------ |
| 上下文感知 | 会话历史存储        | 轻量级对话记忆管理，完整对话历史作为LLM上下文输入            |
| 专家路由   | LLM prompt+规则路由 | 基于提示工程的意图识别 → 专家Agent动态分发，支持议价/技术/客服多场景切换 |

### 业务功能矩阵
| 模块     | 已实现                        | 规划中                       |
| -------- | ----------------------------- | ---------------------------- |
| 核心引擎 | ✅ LLM自动回复<br>✅ 上下文管理 | 🔄 情感分析增强               |
| 议价系统 | ✅ 阶梯降价策略                | 🔄 市场比价功能               |
| 技术支持 | ✅ 网络搜索整合                | 🔄 RAG知识库增强              |
| 运维监控 | ✅ 基础日志                    | 🔄 钉钉集成<br>🔄  Web管理界面 |

## 🎨效果图
<div align="center">
  <img src="./images/demo1.png" width="600" alt="客服">
  <br>
  <em>图1: 客服随叫随到</em>
</div>


<div align="center">
  <img src="./images/demo2.png" width="600" alt="议价专家">
  <br>
  <em>图2: 阶梯式议价</em>
</div>

<div align="center">
  <img src="./images/demo3.png" width="600" alt="技术专家"> 
  <br>
  <em>图3: 技术专家上场</em>
</div>

<div align="center">
  <img src="./images/log.png" width="600" alt="后台log"> 
  <br>
  <em>图4: 后台log</em>
</div>


## 🚴 快速开始
也可以看项目内中文指导书：`docs/guide/USER_GUIDE_ZH.md`
### 环境要求
- Python 3.8+

### 安装步骤
```bash
1. 克隆仓库
git clone https://github.com/<your-account>/XianyuAutoAgent.git
cd XianyuAutoAgent

2. 安装依赖
pip install -r requirements.txt

3. 配置环境变量
创建一个 `.env` 文件，包含以下内容，也可直接重命名 `.env.example` ：
#必配配置
API_KEY=apikey通过模型平台获取
COOKIES_STR=填写网页端获取的cookie
MODEL_BASE_URL=模型地址
MODEL_NAME=模型名称
#可选配置
TOGGLE_KEYWORDS=接管模式切换关键词，默认为句号（输入句号切换为人工接管，再次输入则切换AI接管）
SIMULATE_HUMAN_TYPING=True/False #模拟人工回复延迟

注意：默认使用的模型是通义千问，如需使用其他API，请自行修改.env文件中的模型地址和模型名称；
COOKIES_STR自行在闲鱼网页端获取cookies(网页端F12打开控制台，选择Network，点击Fetch/XHR,点击一个请求，查看cookies)

4. 创建提示词文件prompts/*_prompt.txt（也可以直接将模板名称中的_example去掉），否则默认读取四个提示词模板中的内容
```

### 使用方法

运行主程序：
```bash
python main.py
```

## Event-Action Pipeline（通用事件流水线）

### 架构说明
- **旧行为**：`main.py` 内硬编码分支处理订单提醒和聊天回复。
- **新行为**：`Raw Message -> Event Parser -> Event Handlers -> Actions -> Action Executor`。
- **目标**：核心仓库保持通用，不内置特定业务（如自动答题/发货规则）；业务决策由外部服务（Webhook）或插件处理器完成。

### 默认兼容策略
- 不配置 webhook 时，内置 `ChatAutoReplyHandler` 仍会自动聊天。
- 仅当配置 `EVENT_HANDLERS` 启用外部处理器时，才会执行额外插件逻辑。
- `EVENT_WEBHOOK_ENABLED=false` 时不会发起 webhook 请求。

### 环境变量（新增）
```bash
# 处理器注册（逗号分隔的类路径）
EVENT_HANDLERS=

# webhook 处理器（可选）
EVENT_WEBHOOK_ENABLED=false
EVENT_WEBHOOK_URL=
EVENT_WEBHOOK_TIMEOUT_MS=3000
EVENT_WEBHOOK_RETRIES=2
EVENT_WEBHOOK_SECRET=

# 事件去重 TTL（秒）
EVENT_DEDUP_TTL_SECONDS=86400

# 订单按商品路由（可选）
ORDER_ROUTER_ENABLED=true
ORDER_ITEM_WEBHOOK_ROUTES={}
ORDER_GROUP_WEBHOOK_ROUTES={}
ORDER_ROUTER_TIMEOUT_MS=3000
ORDER_ROUTER_RETRIES=2

# 异步任务轮询（订单系统返回 track_async_task 时启用）
ASYNC_TASK_POLL_ENABLED=true
ASYNC_TASK_POLL_INTERVAL_SECONDS=5
ASYNC_TASK_POLL_TIMEOUT_MS=3000
ASYNC_TASK_POLL_BATCH_SIZE=20
```

### 按商品路由到不同订单业务服务
`ORDER_ITEM_WEBHOOK_ROUTES` 使用 JSON 配置，不需要改代码即可新增商品路由：

```json
{
  "itemA": {"url": "https://biz-a.example.com/events", "secret": "secret-a"},
  "itemB": {"url": "https://biz-b.example.com/events", "secret": "secret-b", "retries": 3}
}
```

- 命中配置的商品：订单事件会发送到对应 webhook，由业务服务返回动作。
- 未命中配置的商品：订单事件跳过业务 webhook，聊天仍由默认 AI 自动回复。
- `item_id` 缺失时会尝试按 `chat_id` 回填最近商品映射，再进行路由。

也支持“分组路由”（一个配置覆盖多个商品）：

```json
{
  "groupA": {
    "items": ["item1", "item2", "item3"],
    "url": "https://biz-a.example.com/events",
    "secret": "group-secret-a"
  }
}
```

优先级：**单商品路由 (`ORDER_ITEM_WEBHOOK_ROUTES`) 高于分组路由 (`ORDER_GROUP_WEBHOOK_ROUTES`)**。

### 异步订单任务（登录链接 / 轮询结果）
如果你的订单系统在下单后会先返回登录链接，再异步完成后续处理，可以让订单 webhook 一次性返回两个动作：

```json
{
  "actions": [
    {
      "action_type": "send_text",
      "payload": {
        "chat_id": "chat123",
        "to_user_id": "user123",
        "text": "请先点击链接完成登录：https://example.com/login/abc"
      }
    },
    {
      "action_type": "track_async_task",
      "payload": {
        "task_id": "task_abc",
        "chat_id": "chat123",
        "to_user_id": "user123",
        "item_id": "itemA",
        "status": "await_login",
        "status_url": "https://biz-a.example.com/tasks/task_abc",
        "poll_interval_seconds": 5
      }
    }
  ]
}
```

- `track_async_task` 只会对返回了该动作的订单生效，不会影响其他商品。
- Agent 会把任务写入本地 SQLite，并在后台轮询 `status_url`。
- 当轮询结果里的 `status` 变化时，若响应携带 `message` 或 `actions`，Agent 会只通知一次。
- 终态建议使用：`passed`、`failed`、`timeout`。

### 事件/动作契约文档
- 事件 schema 与样例：`docs/events.md`
- 动作 schema 与样例（包含 `send_text`）：`docs/actions.md`
- webhook 集成示例（含签名校验）：`docs/integration/webhook-example.md`

### 自定义提示词

可以通过编辑 `prompts` 目录下的文件来自定义各个专家的提示词：

- `classify_prompt.txt`: 意图分类提示词
- `price_prompt.txt`: 价格专家提示词
- `tech_prompt.txt`: 技术专家提示词
- `default_prompt.txt`: 默认回复提示词

## 🤝 参与贡献

欢迎通过 Issue 提交建议或 PR 贡献代码。

## 📄 License

本项目采用仓库内 [LICENSE](./LICENSE) 许可协议。

## 🛡 免责声明

本项目仅用于学习与技术研究，请遵守所在地区法律法规及平台规则。
