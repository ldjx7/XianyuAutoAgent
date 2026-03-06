import os
import sys
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

loguru_stub = ModuleType("loguru")
loguru_stub.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)
sys.modules.setdefault("loguru", loguru_stub)

from XianyuAgent import TechAgent


class _FakeCompletions:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                )
            ]
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


class TechAgentProviderCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.agent = TechAgent(self.client, "tech prompt", lambda text: text)

    def test_tech_agent_uses_dashscope_search_flag(self):
        with patch.dict(
            os.environ,
            {
                "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "MODEL_NAME": "qwen-max",
            },
            clear=False,
        ):
            self.agent.generate("参数怎么样", "功放", "user: 在吗")

        self.assertEqual(
            self.client.chat.completions.last_kwargs["extra_body"],
            {"enable_search": True},
        )

    def test_tech_agent_uses_openrouter_web_plugin(self):
        with patch.dict(
            os.environ,
            {
                "MODEL_BASE_URL": "https://openrouter.ai/api/v1",
                "MODEL_NAME": "openrouter/auto",
            },
            clear=False,
        ):
            self.agent.generate("参数怎么样", "功放", "user: 在吗")

        self.assertEqual(
            self.client.chat.completions.last_kwargs["extra_body"],
            {"plugins": [{"id": "web"}]},
        )


if __name__ == "__main__":
    unittest.main()
