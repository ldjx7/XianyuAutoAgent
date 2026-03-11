import sys
import unittest
from types import ModuleType


openai_stub = ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

from scripts.benchmark_coding_plan_models import (
    DEFAULT_MODELS,
    BenchmarkResult,
    aggregate_results,
    resolve_base_url,
    resolve_models,
)


class CodingPlanBenchmarkTest(unittest.TestCase):
    def test_resolve_models_returns_defaults_when_not_provided(self):
        self.assertEqual(resolve_models(None), DEFAULT_MODELS)

    def test_resolve_models_splits_and_trims_user_input(self):
        self.assertEqual(
            resolve_models(" qwen3.5-plus, glm-5 , kimi-k2.5 "),
            ["qwen3.5-plus", "glm-5", "kimi-k2.5"],
        )

    def test_resolve_base_url_returns_coding_plan_default(self):
        self.assertEqual(
            resolve_base_url(None),
            "https://coding.dashscope.aliyuncs.com/v1",
        )

    def test_aggregate_results_summarizes_average_latency_and_failures(self):
        summary = aggregate_results(
            [
                BenchmarkResult(
                    model="qwen3.5-plus",
                    case_name="default",
                    round_index=1,
                    latency_seconds=1.2,
                    first_token_latency_seconds=0.4,
                    success=True,
                    output_chars=12,
                ),
                BenchmarkResult(
                    model="qwen3.5-plus",
                    case_name="price",
                    round_index=1,
                    latency_seconds=1.8,
                    first_token_latency_seconds=0.6,
                    success=True,
                    output_chars=18,
                ),
                BenchmarkResult(
                    model="glm-5",
                    case_name="default",
                    round_index=1,
                    latency_seconds=None,
                    first_token_latency_seconds=None,
                    success=False,
                    error="timeout",
                    output_chars=0,
                ),
            ]
        )

        self.assertEqual(summary[0]["model"], "qwen3.5-plus")
        self.assertAlmostEqual(summary[0]["avg_latency_seconds"], 1.5)
        self.assertAlmostEqual(summary[0]["avg_first_token_latency_seconds"], 0.5)
        self.assertEqual(summary[0]["successes"], 2)
        self.assertEqual(summary[0]["failures"], 0)

        self.assertEqual(summary[1]["model"], "glm-5")
        self.assertIsNone(summary[1]["avg_latency_seconds"])
        self.assertEqual(summary[1]["successes"], 0)
        self.assertEqual(summary[1]["failures"], 1)


if __name__ == "__main__":
    unittest.main()
