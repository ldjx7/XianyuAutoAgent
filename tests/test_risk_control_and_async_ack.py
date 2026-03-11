import os
import sys
import tempfile
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


openai_stub = ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

loguru_stub = ModuleType("loguru")
loguru_stub.logger = SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
    success=lambda *args, **kwargs: None,
    remove=lambda *args, **kwargs: None,
    add=lambda *args, **kwargs: None,
)
sys.modules.setdefault("loguru", loguru_stub)

dotenv_stub = ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *args, **kwargs: None
dotenv_stub.set_key = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv_stub)

from core.async_task_poller import AsyncTaskPoller
from core.async_task_store import AsyncTaskStore
from core.models import Action, Event
from main import XianyuLive


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class RiskControlAndAsyncAckTest(unittest.TestCase):
    def test_reconnect_uses_risk_backoff_even_with_cached_token(self):
        live = XianyuLive("unb=123; _m_h5_tk=token_123_456")
        live.current_token = "cached-token"
        live.risk_control_retry_interval = 1800
        live.last_reconnect_reason = "risk_control"

        self.assertEqual(live.get_reconnect_delay_seconds(), 1800)

    def test_async_task_final_status_is_not_acknowledged_until_delivery(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = os.path.join(temp_dir.name, "chat_history.db")
        store = AsyncTaskStore(db_path=db_path)
        store.upsert_task(
            {
                "task_id": "task-1",
                "chat_id": "chat-1",
                "to_user_id": "buyer-1",
                "item_id": "item-1",
                "status": "await_login",
                "status_url": "https://order.example.com/tasks/task-1",
                "poll_interval_seconds": 5,
                "next_poll_at": int(time.time()) - 1,
            }
        )
        poller = AsyncTaskPoller(store=store, timeout_ms=1500)
        response = _FakeResponse(
            200,
            {
                "status": "passed",
                "message": "考试已通过，稍后把结果发你。",
            },
        )

        with patch("core.async_task_poller.requests.request", return_value=response):
            pending = poller.poll_due_tasks(now_ts=int(time.time()))

        self.assertEqual(len(pending), 1)
        saved = store.get_task("task-1")
        self.assertEqual(saved["status"], "passed")
        self.assertIsNone(saved["completed_at"])
        self.assertEqual(saved["last_notified_status"], "await_login")

        poller.acknowledge_delivered(pending[0], delivered_at=int(time.time()))
        saved_after_ack = store.get_task("task-1")
        self.assertEqual(saved_after_ack["last_notified_status"], "passed")
        self.assertIsNotNone(saved_after_ack["completed_at"])

    def test_generate_bot_reply_async_times_out(self):
        class _SlowBot:
            def generate_reply(self, *args, **kwargs):
                time.sleep(0.05)
                return "late"

        live = XianyuLive("unb=123; _m_h5_tk=token_123_456")
        live.llm_request_timeout_seconds = 0.01

        result = __import__("asyncio").run(
            live.generate_bot_reply_async(_SlowBot(), "你好", "商品描述", [])
        )

        self.assertIsNone(result)

    def test_pipeline_awaits_async_handlers(self):
        class _AsyncHandler:
            name = "async_handler"

            async def handle(self, event):
                return [Action(action_type="set_manual_mode", payload={"chat_id": "chat-1", "enabled": True})]

        class _RecorderExecutor:
            def __init__(self):
                self.actions = []
                self.context = None

            async def execute(self, actions, context=None):
                self.actions = list(actions)
                self.context = context

        live = XianyuLive("unb=123; _m_h5_tk=token_123_456")
        live.event_handlers = [_AsyncHandler()]
        recorder = _RecorderExecutor()
        live.action_executor = recorder
        event = Event(
            event_id=f"evt-async-handler-{time.time_ns()}",
            event_type="chat.message.received",
            occurred_at=int(time.time()),
            payload={"chat_id": "chat-1", "user_id": "buyer-1", "message": "你好", "item_id": "item-1"},
        )

        async def _run():
            with patch("main.parse_events", return_value=[event]):
                await live.handle_pipeline_message({}, websocket="ws")

        __import__("asyncio").run(_run())

        self.assertEqual(len(recorder.actions), 1)
        self.assertEqual(recorder.actions[0].action_type, "set_manual_mode")
        self.assertEqual(recorder.context, {"websocket": "ws"})


if __name__ == "__main__":
    unittest.main()
