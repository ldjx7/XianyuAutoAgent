import json
import unittest
from unittest.mock import patch

from core.handlers.order_route_handler import OrderRouteHandler
from core.models import Event


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class OrderRouteHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = OrderRouteHandler(
            routes={"1032205428219": {"url": "http://example.com/order-start"}},
            enabled=True,
            default_timeout_ms=1000,
            default_retries=0,
        )

    def _build_event(self, order_status):
        return Event(
            event_id=f"evt-{order_status}",
            event_type="order.status.changed",
            occurred_at=1773223253000,
            payload={
                "chat_id": "59178533554",
                "user_id": "2222127989978",
                "item_id": "1032205428219",
                "order_status": order_status,
                "raw": {"message": "system"},
                "websocket": object(),
            },
        )

    def test_paid_status_routes_and_sanitizes_payload(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            json_module = __import__("json")
            json_module.dumps(json, ensure_ascii=False)
            return _FakeResponse(
                200,
                {
                    "actions": [
                        {
                            "action_type": "send_text",
                            "payload": {
                                "chat_id": "59178533554",
                                "to_user_id": "2222127989978",
                                "text": "请扫码登录",
                            },
                        }
                    ]
                },
            )

        with patch("core.handlers.order_route_handler.requests.post", side_effect=fake_post):
            actions = self.handler.handle(self._build_event("我已付款，等待你发货"))

        self.assertEqual(len(actions), 1)
        self.assertEqual(captured["url"], "http://example.com/order-start")
        self.assertNotIn("websocket", captured["json"]["payload"])
        self.assertEqual(captured["json"]["payload"]["order_status"], "我已付款，等待你发货")

    def test_refund_and_unpaid_statuses_do_not_route(self):
        statuses = [
            "我发起了退款申请",
            "我已拍下，待付款",
            "未付款，买家关闭了订单",
        ]

        for status in statuses:
            with self.subTest(status=status):
                with patch("core.handlers.order_route_handler.requests.post") as mocked_post:
                    actions = self.handler.handle(self._build_event(status))

                self.assertEqual(actions, [])
                mocked_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
