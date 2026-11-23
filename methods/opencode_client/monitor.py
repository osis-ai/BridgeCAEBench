"""OSIS-AI 反问监控器。

场景:opencode 跑 OSIS-AI 时,如果 prompt 信息不足,模型可能调内置 question
工具反问。chat() 是阻塞等回复的,反问时 session 会被 server 挂起,chat() 永远
不返回。本模块提供 QuestionMonitor:在后台线程里轮询 question 队列,按 strategy
自动答,让 chat() 能正常返回。

策略:
- "first":  每个 question 选 options[0](适合调试期 / OSIS-AI 还在迭代期)
- 未来扩展:加 AI 决策器、规则匹配、缓存复用
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .client import OpencodeClient, OpencodeError


Strategy = Literal["first", "all_first"]

# 回调签名:on_question(action, q) → None
#   action: "seen" | "replied" | "rejected" | "error"
#   q: opencode 那个 question dict(原样,带 id/sessionID/questions 等)
#   detail: 可选附注(reply 时是答案 list;reject/error 时是 reason)
QuestionCallback = Callable[[str, dict[str, Any], Any], None]


@dataclass
class AutoReplyStats:
    """monitor 累计统计,跑完后用于诊断。"""
    seen: int = 0              # 看到的 question 数
    replied: int = 0           # 成功 reply 的(包括无 options 的占位)
    rejected: int = 0          # reply 失败后 fallback reject 的(应≈0)
    errors: int = 0            # 通讯/解析错误次数


def _build_first_answers(question_info: dict) -> list[str]:
    """单个 QuestionInfo → 答案 string[]。

    永远返非空 list:
    - 有 options + 单选 → [options[0].label]
    - 有 options + 多选 → 所有 label
    - 无 options(text-input 类,如"项目目录在哪") → ["OK"](占位,让 OSIS-AI 继续)
    """
    options = question_info.get("options", []) or []
    if options:
        if question_info.get("multiple"):
            return [o["label"] for o in options]
        return [options[0]["label"]]
    # text-input question:无 options,给一个默认答复,不让 monitor 走 reject
    return ["OK"]


class QuestionMonitor:
    """后台线程:轮询指定 session 的待回答 question,按 strategy 自动答。

    用法::

        monitor = QuestionMonitor(client, session_id, strategy="first")
        monitor.start()
        try:
            msg = client.chat(session_id, prompt)  # 阻塞;遇到 question 会挂起
        finally:
            monitor.stop()
        stats = monitor.stats
    """

    def __init__(
        self,
        client: OpencodeClient,
        session_id: str,
        *,
        strategy: Strategy = "first",
        poll_interval: float = 1.0,
        log=print,
        on_question: QuestionCallback | None = None,
    ):
        self._client = client
        self._session_id = session_id
        self._strategy = strategy
        self._poll_interval = poll_interval
        self._log = log
        self._on_question = on_question
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = AutoReplyStats()

    def _notify(self, action: str, q: dict, detail: Any = None) -> None:
        if self._on_question is None:
            return
        try:
            self._on_question(action, q, detail)
        except Exception:
            pass  # 回调异常不能杀 monitor 线程

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"qmon-{self._session_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        seen_ids: set[str] = set()
        while not self._stop.is_set():
            try:
                # 用全局端点,客户端按 sessionID filter(v2 session 端点不稳)
                for q in self._client.list_global_questions():
                    if q.get("sessionID") != self._session_id:
                        continue
                    qid = q.get("id", "")
                    if not qid or qid in seen_ids:
                        continue
                    seen_ids.add(qid)
                    self.stats.seen += 1
                    self._notify("seen", q)
                    self._handle_one(q)
            except OpencodeError as e:
                self.stats.errors += 1
                self._log(f"[monitor] 轮询出错:{e}", file=sys.stderr)
                self._notify("error", {}, str(e))
            except Exception as e:  # 兜底,不杀线程
                self.stats.errors += 1
                self._log(f"[monitor] 异常:{e}", file=sys.stderr)
                self._notify("error", {}, str(e))

            # 退避:支持 stop_event 提前唤醒
            self._stop.wait(self._poll_interval)

    def _handle_one(self, q: dict) -> None:
        qid = q["id"]
        infos: list[dict] = q.get("questions", []) or []
        # 永远走 reply 路径:无 options 的 text-input question 也给个 "OK" 占位,
        # 让 OSIS-AI 继续,而不是被 reject 立即结束 session。
        answers: list[list[str]] = [_build_first_answers(info) for info in infos]

        try:
            self._client.reply_question(qid, answers)
            self.stats.replied += 1
            self._log(
                f"[monitor] reply {qid}:{answers}",
                file=sys.stderr,
            )
            self._notify("replied", q, answers)
        except OpencodeError as e:
            # reply 真失败时(API 错/格式错),fallback to reject,
            # 让 session 至少能走完而不是无限挂起。
            self.stats.errors += 1
            self._log(f"[monitor] reply {qid} 失败:{e};fallback to reject", file=sys.stderr)
            self._notify("error", q, f"reply 失败:{e}")
            try:
                self._client.reject_question(qid)
                self.stats.rejected += 1
                self._notify("rejected", q, "reply failed, fallback")
            except OpencodeError as e2:
                self._log(f"[monitor] reject {qid} 也失败:{e2}", file=sys.stderr)