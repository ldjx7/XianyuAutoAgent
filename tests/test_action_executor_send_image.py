import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace


loguru_stub = ModuleType("loguru")
loguru_stub.logger = SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules.setdefault("loguru", loguru_stub)

from core.action_executor import ActionExecutor
from core.models import Action


class ActionExecutorSendImageTest(unittest.TestCase):
    def test_send_image_uses_image_sender_when_configured(self):
        sent_images = []
        sent_texts = []

        async def send_msg(*args):
            sent_texts.append(args)

        async def send_image(*args):
            sent_images.append(args)

        executor = ActionExecutor(
            send_msg_func=send_msg,
            send_image_func=send_image,
            set_manual_mode_func=None,
            track_async_task_func=None,
        )

        action = Action(
            action_type="send_image",
            payload={
                "chat_id": "chat-1",
                "to_user_id": "buyer-1",
                "image_url": "https://img.example.com/qr.png",
                "text": "请扫码登录",
            },
        )

        asyncio.run(executor.execute([action], context={"websocket": "ws"}))

        self.assertEqual(
            sent_images,
            [("ws", "chat-1", "buyer-1", "https://img.example.com/qr.png", "请扫码登录")],
        )
        self.assertEqual(sent_texts, [])

    def test_send_image_falls_back_to_text_when_image_sender_missing(self):
        sent_texts = []

        async def send_msg(*args):
            sent_texts.append(args)

        executor = ActionExecutor(
            send_msg_func=send_msg,
            set_manual_mode_func=None,
            track_async_task_func=None,
        )

        action = Action(
            action_type="send_image",
            payload={
                "chat_id": "chat-1",
                "to_user_id": "buyer-1",
                "image_url": "https://img.example.com/qr.png",
                "fallback_text": "请扫码登录",
            },
        )

        asyncio.run(executor.execute([action], context={"websocket": "ws"}))

        self.assertEqual(
            sent_texts,
            [("ws", "chat-1", "buyer-1", "请扫码登录\nhttps://img.example.com/qr.png")],
        )


if __name__ == "__main__":
    unittest.main()
