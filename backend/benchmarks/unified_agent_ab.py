"""Real SSE A/B runner for the unified knowledge Agent.

Run two presets against the same KB and prompt. The runner records wall-clock
latency, first-answer latency, tool sequence, retrieval audits, and token usage.

Usage:
    cd backend && ../.venv/bin/python -m benchmarks.unified_agent_ab \
      --kb-id <kb_id> \
      --preset-a <hybrid-baseline-preset-id> \
      --preset-b <summary-artifact-preset-id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import httpx

DEFAULT_QUERY = (
    "请总结知识库文档的核心要点，并标注每条来自哪份文件。知识库内一共6、7个文件？"
)


async def _login(client: httpx.AsyncClient, base_url: str, username: str, password: str) -> str:
    response = await client.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def run_case(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    kb_id: str,
    preset_id: str,
    query: str,
) -> dict:
    started = time.perf_counter()
    first_text_ms: int | None = None
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    text_parts: list[str] = []
    token_usage: list[dict] = []
    errors: list[str] = []
    complete: dict = {}

    payload = {
        "model": "rag",
        "messages": [{"role": "user", "content": query}],
        "stream": True,
        "knowledge_base_id": kb_id,
        "agent_preset_id": preset_id,
        "timezone_name": "Asia/Shanghai",
    }
    async with client.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            event = json.loads(raw)
            event_type = event.get("type")
            if event_type == "text_delta":
                if first_text_ms is None:
                    first_text_ms = round((time.perf_counter() - started) * 1000)
                text_parts.append(event.get("content", ""))
            elif event_type == "tool_call":
                tool_calls.append(
                    {
                        "tool_name": event.get("tool_name"),
                        "arguments": event.get("arguments"),
                        "iteration": event.get("iteration"),
                    }
                )
            elif event_type == "tool_result":
                tool_results.append(
                    {
                        "tool_name": event.get("tool_name"),
                        "success": event.get("success"),
                        "duration_ms": event.get("duration_ms"),
                        "files": event.get("files", []),
                        "retrieval_audit": event.get("retrieval_audit"),
                    }
                )
            elif event_type == "token_usage":
                token_usage.append(event)
            elif event_type == "error":
                errors.append(event.get("content", ""))
            elif event_type == "complete":
                complete = event

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    answer = "".join(text_parts)
    retrieval_audits = [
        result["retrieval_audit"]
        for result in tool_results
        if result.get("retrieval_audit")
    ]
    return {
        "preset_id": preset_id,
        "elapsed_ms": elapsed_ms,
        "first_text_ms": first_text_ms,
        "complete": complete,
        "tool_call_count": len(tool_calls),
        "tool_sequence": [
            (
                call.get("tool_name"),
                (call.get("arguments") or {}).get("mode"),
                (call.get("arguments") or {}).get("query"),
            )
            for call in tool_calls
        ],
        "tool_results": tool_results,
        "retrieval_calls": len(retrieval_audits),
        "retrieval_audits": retrieval_audits,
        "retrieved_file_ids": sorted(
            {
                file.get("id")
                for result in tool_results
                for file in result.get("files", [])
                if file.get("id")
            }
        ),
        "token_usage": token_usage,
        "errors": errors,
        "answer_chars": len(answer),
        "answer_head": answer[:500],
    }


async def run(args) -> dict:
    async with httpx.AsyncClient(timeout=None) as client:
        token = await _login(client, args.base_url, args.username, args.password)
        case_a = await run_case(
            client=client,
            base_url=args.base_url,
            token=token,
            kb_id=args.kb_id,
            preset_id=args.preset_a,
            query=args.query,
        )
        case_b = await run_case(
            client=client,
            base_url=args.base_url,
            token=token,
            kb_id=args.kb_id,
            preset_id=args.preset_b,
            query=args.query,
        )
    return {
        "query": args.query,
        "case_a": case_a,
        "case_b": case_b,
        "improvement": {
            "elapsed_reduction_percent": round(
                (case_a["elapsed_ms"] - case_b["elapsed_ms"])
                / max(1, case_a["elapsed_ms"])
                * 100,
                1,
            ),
            "first_text_reduction_percent": round(
                ((case_a["first_text_ms"] or case_a["elapsed_ms"]) - (case_b["first_text_ms"] or case_b["elapsed_ms"]))
                / max(1, case_a["first_text_ms"] or case_a["elapsed_ms"])
                * 100,
                1,
            ),
            "tool_call_reduction": case_a["tool_call_count"] - case_b["tool_call_count"],
            "file_coverage_delta": len(case_b["retrieved_file_ids"])
            - len(case_a["retrieved_file_ids"]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.getenv("ARTOO_ADMIN_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("ARTOO_ADMIN_PASSWORD", ""))
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--preset-a", required=True)
    parser.add_argument("--preset-b", required=True)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()
    if not args.username or not args.password:
        parser.error(
            "set --username/--password or ARTOO_ADMIN_USERNAME/ARTOO_ADMIN_PASSWORD"
        )
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
