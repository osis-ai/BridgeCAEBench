"""T5: the official OSIS-AI agent: one OpenCode session per run, driven by methods/opencode_client.

Each run starts its own OSIS-AI server (bench.osisai) from the install pack: the campaign skill
snapshot, the platform gateway model and the run's MCP servers, both through the run's recorders.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from bench import osisai
from methods._common import base_metadata, end_turn, fail, finish, main, normalize_tokens

# the simulated engineer: whatever OSIS-AI asks, the agent decides itself (T2 OpenCode gets the same answer; methods that cannot ask decide anyway)
DEFAULT_ANSWER = "按默认参数，你自己判断"


def chat(request: dict[str, Any]) -> dict[str, Any]:
    """One OpenCode session with the task's instructions; OSIS-AI writes and runs the project itself."""
    from methods.opencode_client import OpencodeClient, OpencodeError, QuestionMonitor, extract_stats, extract_text

    log = Path(request["run_dir"]) / "questions.jsonl"

    class DefaultAnswers(QuestionMonitor):
        def _handle_one(self, q: dict) -> None:
            infos = q.get("questions", []) or []
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.time(), "questions": infos, "answer": DEFAULT_ANSWER}, ensure_ascii=False) + "\n")
            try:
                self._client.reply_question(q["id"], [[DEFAULT_ANSWER] for _ in infos])
                self.stats.replied += 1
            except OpencodeError:  # e.g. options without free text: fall back to the first option
                super()._handle_one(q)

    client = OpencodeClient(base_url=request["opencode_url"], timeout=float(request["timeout_s"]))
    sid = client.create_session(f"{request['task']}:{request['item_id']}")
    print(f"[osisai] session {sid} at {request['opencode_url']}", flush=True)
    monitor = DefaultAnswers(client, sid, strategy="first", poll_interval=1.0)
    started = time.perf_counter()
    monitor.start()
    try:
        for text in [request["instructions"], *(request.get("turns") or [])]:  # project: the same session per turn
            message = client.chat(sid, text, provider_id=osisai.PROVIDER, model_id=request["model"]["name"],
                                  timeout=float(request["timeout_s"]))
            answer = extract_text(message) if message is not None else ""
            if not answer.strip():
                break
            end_turn(request, answer)
    finally:
        monitor.stop()
    stats = extract_stats(message) if message is not None else {}
    return {"raw": extract_text(message) if message is not None else "",
            "latency_s": round(time.perf_counter() - started, 3),
            "usage": {"prompt_tokens": stats.get("input_tokens"), "completion_tokens": stats.get("output_tokens"),
                      "total_tokens": stats.get("total_tokens")}}


def run(request: dict[str, Any], *, chat_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    metadata = base_metadata(request, framework="osis-ai-native", interaction_mode="parent_session",
                             skill_loading="run_osisai_server")
    try:
        if chat_fn is None:
            with osisai.Server(request) as server:
                output = chat({**request, "opencode_url": server.url})
        else:
            output = chat_fn(request)
        answer = str(output.get("raw") or output.get("final_answer") or "").strip()
        if not answer:
            raise RuntimeError("OSIS-AI returned no final answer")
        metadata.update(final_answer=answer, tokens=normalize_tokens(output.get("usage")),
                        latency_s=output.get("latency_s"), status="completed", stop_reason="completed")
    except Exception as exc:  # noqa: BLE001
        fail(metadata, exc)
    return finish(request, metadata, started)


if __name__ == "__main__":
    raise SystemExit(main(run))
