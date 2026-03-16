import unittest

from core.event_parser import parse_events


class EventParserOrderStatusTests(unittest.TestCase):
    def test_parse_events_reads_order_status_from_red_reminder(self):
        message = {
            "1": {
                "2": "chat-1@goofish",
                "5": "1773223253000",
                "10": {
                    "senderUserId": "buyer-1@goofish",
                    "reminderContent": "[我已付款，等待你发货]",
                    "reminderTitle": "等待你发货",
                    "reminderUrl": "https://www.goofish.com/?itemId=1032205428219",
                },
            },
            "2": "chat-1@goofish",
            "3": {
                "redReminder": "等待卖家发货",
            },
        }

        events = parse_events(message)
        order_events = [event for event in events if event.event_type == "order.status.changed"]

        self.assertEqual(len(order_events), 1)
        self.assertEqual(order_events[0].payload["order_status"], "等待卖家发货")

    def test_parse_events_falls_back_to_bracket_order_message_when_red_reminder_missing(self):
        message = {
            "1": {
                "2": "59178533554@goofish",
                "5": "1773223253000",
                "10": {
                    "senderUserId": "2222127989978@goofish",
                    "reminderContent": "[我已付款，等待你发货]",
                    "reminderTitle": "等待你发货",
                    "reminderUrl": "https://www.goofish.com/?itemId=1032205428219",
                },
            }
        }

        events = parse_events(message)
        order_events = [event for event in events if event.event_type == "order.status.changed"]

        self.assertEqual(len(order_events), 1)
        self.assertEqual(order_events[0].payload["chat_id"], "59178533554")
        self.assertEqual(order_events[0].payload["user_id"], "2222127989978")
        self.assertEqual(order_events[0].payload["order_status"], "我已付款，等待你发货")


if __name__ == "__main__":
    unittest.main()
