import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional

from openai import OpenAI


DEFAULT_BASE_URL = "https://coding.dashscope.aliyuncs.com/v1"
DEFAULT_MODELS = [
    "qwen3.5-plus",
    "qwen3-max-2026-01-23",
    "qwen3-coder-next",
    "qwen3-coder-plus",
    "glm-5",
    "glm-4.7",
    "kimi-k2.5",
    "MiniMax-M2.5",
]

DEFAULT_CASES = [
    {
        "name": "default",
        "messages": [
            {"role": "system", "content": "你是闲鱼卖家客服助手，请用自然简洁的中文回复。"},
            {"role": "user", "content": "你好，这个商品还在吗？什么时候可以发货？"},
        ],
    },
    {
        "name": "price",
        "messages": [
            {"role": "system", "content": "你是闲鱼卖家客服助手，请用自然简洁的中文回复。"},
            {"role": "user", "content": "可以便宜一点吗？诚心要，今天就拍。"},
        ],
    },
    {
        "name": "tech",
        "messages": [
            {"role": "system", "content": "你是闲鱼卖家客服助手，请用自然简洁的中文回复。"},
            {"role": "user", "content": "这个服务具体怎么使用？需要我提供哪些信息？"},
        ],
    },
]


@dataclass
class BenchmarkResult:
    model: str
    case_name: str
    round_index: int
    latency_seconds: Optional[float]
    first_token_latency_seconds: Optional[float]
    success: bool
    output_chars: int
    error: str = ""
    preview: str = ""


def resolve_base_url(raw_base_url: Optional[str]) -> str:
    if raw_base_url:
        return raw_base_url
    return DEFAULT_BASE_URL


def resolve_models(raw_models: Optional[str]) -> List[str]:
    if not raw_models:
        return list(DEFAULT_MODELS)
    models = [item.strip() for item in raw_models.split(",") if item.strip()]
    return models or list(DEFAULT_MODELS)


def aggregate_results(results: Iterable[BenchmarkResult]) -> List[Dict[str, Optional[float]]]:
    grouped: Dict[str, Dict[str, object]] = {}
    for result in results:
        bucket = grouped.setdefault(
            result.model,
            {
                "model": result.model,
                "latencies": [],
                "first_token_latencies": [],
                "successes": 0,
                "failures": 0,
                "output_chars": 0,
            },
        )
        if result.success and result.latency_seconds is not None:
            bucket["latencies"].append(result.latency_seconds)
            bucket["successes"] += 1
            bucket["output_chars"] += result.output_chars
        else:
            bucket["failures"] += 1
        if result.success and result.first_token_latency_seconds is not None:
            bucket["first_token_latencies"].append(result.first_token_latency_seconds)

    summary = []
    for model, bucket in grouped.items():
        latencies = bucket["latencies"]
        first_token_latencies = bucket["first_token_latencies"]
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        min_latency = min(latencies) if latencies else None
        max_latency = max(latencies) if latencies else None
        avg_first = sum(first_token_latencies) / len(first_token_latencies) if first_token_latencies else None
        summary.append(
            {
                "model": model,
                "avg_first_token_latency_seconds": avg_first,
                "avg_latency_seconds": avg_latency,
                "min_latency_seconds": min_latency,
                "max_latency_seconds": max_latency,
                "successes": bucket["successes"],
                "failures": bucket["failures"],
                "avg_output_chars": (
                    bucket["output_chars"] / bucket["successes"] if bucket["successes"] else 0
                ),
            }
        )

    return sorted(
        summary,
        key=lambda item: (
            item["avg_latency_seconds"] is None,
            item["avg_latency_seconds"] if item["avg_latency_seconds"] is not None else float("inf"),
            item["model"],
        ),
    )


def _format_latency(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}s"


def render_summary_table(summary: List[Dict[str, object]]) -> str:
    headers = ["Model", "AvgFirst", "Avg", "Min", "Max", "Success", "Fail", "AvgChars"]
    rows = [
        [
            item["model"],
            _format_latency(item.get("avg_first_token_latency_seconds")),
            _format_latency(item["avg_latency_seconds"]),
            _format_latency(item["min_latency_seconds"]),
            _format_latency(item["max_latency_seconds"]),
            str(item["successes"]),
            str(item["failures"]),
            f"{item['avg_output_chars']:.0f}",
        ]
        for item in summary
    ]
    return _render_table(headers, rows)


def render_detail_table(results: List[BenchmarkResult]) -> str:
    headers = ["Model", "Case", "Round", "FirstToken", "Latency", "Status", "Chars", "Preview/Error"]
    rows = []
    for result in results:
        rows.append(
            [
                result.model,
                result.case_name,
                str(result.round_index),
                _format_latency(result.first_token_latency_seconds),
                _format_latency(result.latency_seconds),
                "ok" if result.success else "fail",
                str(result.output_chars),
                (result.preview or result.error or "")[:60],
            ]
        )
    return _render_table(headers, rows)


def _render_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def render_row(row: List[str]) -> str:
        return " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    divider = "-+-".join("-" * width for width in widths)
    rendered = [render_row(headers), divider]
    rendered.extend(render_row(row) for row in rows)
    return "\n".join(rendered)


def _extract_text(response) -> str:
    try:
        content = response.choices[0].message.content
    except Exception:
        return ""
    return content or ""


def benchmark_once(
    client: OpenAI,
    model: str,
    case: Dict[str, object],
    round_index: int,
    timeout: float,
    stream: bool = False,
) -> BenchmarkResult:
    started_at = time.perf_counter()
    try:
        if stream:
            response = client.chat.completions.create(
                model=model,
                messages=case["messages"],
                temperature=0.2,
                max_tokens=180,
                top_p=0.8,
                timeout=timeout,
                stream=True,
            )
            text_parts: List[str] = []
            first_token_latency = None
            for chunk in response:
                content = ""
                try:
                    content = chunk.choices[0].delta.content or ""
                except Exception:
                    content = ""
                if content and first_token_latency is None:
                    first_token_latency = time.perf_counter() - started_at
                if content:
                    text_parts.append(content)
            latency = time.perf_counter() - started_at
            text = "".join(text_parts)
        else:
            response = client.chat.completions.create(
                model=model,
                messages=case["messages"],
                temperature=0.2,
                max_tokens=180,
                top_p=0.8,
                timeout=timeout,
            )
            latency = time.perf_counter() - started_at
            first_token_latency = None
            text = _extract_text(response)
        normalized_preview = " ".join(text.split())
        return BenchmarkResult(
            model=model,
            case_name=str(case["name"]),
            round_index=round_index,
            latency_seconds=latency,
            first_token_latency_seconds=first_token_latency,
            success=True,
            output_chars=len(text),
            preview=normalized_preview[:60],
        )
    except Exception as exc:
        return BenchmarkResult(
            model=model,
            case_name=str(case["name"]),
            round_index=round_index,
            latency_seconds=None,
            first_token_latency_seconds=None,
            success=False,
            output_chars=0,
            error=str(exc),
        )


def run_benchmark(
    client: OpenAI,
    models: List[str],
    rounds: int,
    timeout: float,
    stream: bool,
    cases: Optional[List[Dict[str, object]]] = None,
) -> List[BenchmarkResult]:
    selected_cases = cases or list(DEFAULT_CASES)
    results: List[BenchmarkResult] = []
    for model in models:
        for round_index in range(1, rounds + 1):
            for case in selected_cases:
                result = benchmark_once(client, model, case, round_index, timeout, stream=stream)
                results.append(result)
                status = "ok" if result.success else "fail"
                if result.success:
                    detail = f"first={_format_latency(result.first_token_latency_seconds)} total={_format_latency(result.latency_seconds)}"
                else:
                    detail = result.error
                print(
                    f"[{status}] model={result.model} case={result.case_name} round={result.round_index} detail={detail}"
                )
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark DashScope Coding Plan model latency.")
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("API_KEY", ""))
    parser.add_argument("--models", default="")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--json-out", default="")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.api_key:
        parser.error("missing API key, set API_KEY or pass --api-key")

    base_url = resolve_base_url(args.base_url)
    models = resolve_models(args.models)
    client = OpenAI(api_key=args.api_key, base_url=base_url)

    print(f"Base URL : {base_url}")
    print(f"Models   : {', '.join(models)}")
    print(f"Rounds   : {args.rounds}")
    print(f"Cases    : {', '.join(case['name'] for case in DEFAULT_CASES)}")
    print(f"Stream   : {args.stream}")
    print("")

    results = run_benchmark(client, models, args.rounds, args.timeout, stream=args.stream)
    summary = aggregate_results(results)

    print("\nSummary")
    print(render_summary_table(summary))
    print("\nDetails")
    print(render_detail_table(results))

    if args.json_out:
        payload = {
            "base_url": base_url,
            "models": models,
            "rounds": args.rounds,
            "stream": args.stream,
            "cases": [case["name"] for case in DEFAULT_CASES],
            "results": [asdict(result) for result in results],
            "summary": summary,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON results written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
