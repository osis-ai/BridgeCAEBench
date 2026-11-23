"""opencode HTTP 客户端封装

只负责跟 opencode server 通信(创建 session、发送消息、读回复),
与具体业务(OSIS-AI / SKILL / 评价)都不耦合——纯协议层。
后端需自行启动:opencode serve --hostname 127.0.0.1 --port 4096
"""

from __future__ import annotations

import os
from typing import Any

from opencode_ai import Opencode
from opencode_ai import APIError, APIConnectionError


DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_PROVIDER = "opencode"
DEFAULT_MODEL = "deepseek-v4-flash-free"
# 一次"建桥"任务最长能跑到 1h 量级;默认 6000s 留足缓冲。
DEFAULT_TIMEOUT = 6000.0


def _ensure_local_no_proxy() -> None:
    """保证 127.0.0.1 / localhost 在 NO_PROXY 里,httpx/opencode_ai SDK 才会直连。

    Windows 注册表里有 ProxyServer=127.0.0.1:7897 时,httpx 默认走代理;
    ProxyOverride 虽然列了 127.*,但 httpx 不一定正确解析,会把 localhost
    请求也发给 7897(socks5 代理对 127.0.0.1 返 502 Bad Gateway)。
    curl/requests/urllib3 都能正确读 ProxyOverride,所以只 httpx 出问题。

    副作用:只对 127.0.0.1 和 localhost 生效,不破坏对真实 LLM API 的代理。
    """
    needed = {"127.0.0.1", "localhost", "::1"}
    for var in ("NO_PROXY", "no_proxy"):
        current = {p.strip() for p in os.environ.get(var, "").split(",") if p.strip()}
        if needed - current:
            merged = ",".join(sorted(current | needed))
            os.environ[var] = merged


_ensure_local_no_proxy()


class OpencodeError(RuntimeError):
    """调用 opencode server 出错时抛的异常。"""


class OpencodeClient:
    """opencode 会话客户端。

    典型用法::

        client = OpencodeClient()
        sid = client.create_session("first task")
        msg = client.chat(sid, "你好")
        print(extract_text(msg))
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url or os.environ.get("OPENCODE_URL", DEFAULT_BASE_URL)
        self._client = Opencode(base_url=self._base_url, timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._base_url

    # ---- 健康检查 ----

    def health(self) -> bool:
        """探活。/config 是 opencode 必定有的路由,能 GET 即代表 server 在跑。"""
        try:
            self._client.config.get()
        except APIConnectionError as e:
            raise OpencodeError(
                f"连不上 opencode server {self._base_url},请先启动:opencode serve --port 4096"
            ) from e
        except APIError as e:
            raise OpencodeError(f"opencode health check 失败:{e}") from e
        return True

    def version(self) -> str:
        """通过 /global/health 读 server 版本(用 raw HTTP,SDK 没暴露 /global 子路由)。"""
        import httpx
        try:
            r = httpx.get(f"{self._base_url}/global/health", timeout=5.0)
            r.raise_for_status()
            return r.json().get("version", "unknown")
        except Exception as e:
            raise OpencodeError(f"读 server 版本失败:{e}") from e

    # ---- 会话 ----

    def create_session(self, title: str = "opencode") -> str:
        """新建会话,返回 session id。

        SDK 0.1.0a36 的 create() 不直接收 title,走 extra_body 注入。
        """
        try:
            session = self._client.session.create(extra_body={"title": title})
        except APIError as e:
            raise OpencodeError(f"创建 session 失败:{e}") from e
        return session.id

    def list_sessions(self) -> list[Any]:
        try:
            res = self._client.session.list()
        except APIError as e:
            raise OpencodeError(f"列 session 失败:{e}") from e
        return list(res)  # SessionListResponse 实现了 __iter__

    def get_messages(self, session_id: str) -> Any:
        try:
            return self._client.session.messages(id=session_id)
        except APIError as e:
            raise OpencodeError(f"读 messages 失败:{e}") from e

    def delete_session(self, session_id: str) -> bool:
        """删除一个 session;返 True 表示 server 接受。"""
        try:
            self._client.session.delete(session_id)
            return True
        except APIError as e:
            raise OpencodeError(f"删除 session 失败:{e}") from e

    # ---- question(LLM 反问)----

    def list_session_questions(self, session_id: str) -> list[dict[str, Any]]:
        """列指定 session 的待回答 question 列表。

        实现:opencode 1.17.x 的 v2 端点 GET /api/session/{sid}/question 在没
        待回答 question 时会返 400(不返空数组),所以这里走全局 /question 端点
        + 客户端按 sessionID 过滤。这样更稳。

        返回 QuestionRequest 列表,字段:
        - id:            request id(用于 reply/reject)
        - sessionID:     所属 session
        - questions:     QuestionInfo[] = {question, header, options[], multiple, custom}
        - tool:          {messageID, callID}
        """
        return [q for q in self.list_global_questions() if q.get("sessionID") == session_id]

    def list_global_questions(self) -> list[dict[str, Any]]:
        """列全局待回答 question 列表(跨所有 session)。

        用法:监控器拉这个端点,按 sessionID 过滤出自己关心的。
        """
        try:
            r = self._client.get(
                "/question",
                cast_to=object,
            )
        except APIError as e:
            raise OpencodeError(f"列全局 question 失败:{e}") from e
        data: Any = getattr(r, "parsed", r)
        if not isinstance(data, list):
            return []
        return data

    def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        """提交 question 回答。

        answers: 每个 question 一个 string[](因为支持多选,即使单选也是 list);
                 顺序对齐 questions[]。
        返回 True 表示 server 接受。
        """
        try:
            r = self._client.post(
                f"/question/{request_id}/reply",
                body={"answers": answers},
                cast_to=object,
            )
        except APIError as e:
            raise OpencodeError(f"reply question 失败:{e}") from e
        return bool(getattr(r, "parsed", r))

    def reject_question(self, request_id: str) -> bool:
        """拒绝 question(模型继续跑,反问作废)。"""
        try:
            r = self._client.post(
                f"/question/{request_id}/reject",
                cast_to=object,
            )
        except APIError as e:
            raise OpencodeError(f"reject question 失败:{e}") from e
        return bool(getattr(r, "parsed", r))

    # ---- 提示 ----

    def chat(
        self,
        session_id: str,
        text: str,
        provider_id: str = DEFAULT_PROVIDER,
        model_id: str = DEFAULT_MODEL,
        timeout: float | None = None,
        variant: str | None = None,
    ) -> Any:
        """发一条消息,等完整回复(AssistantMessage)。

        SDK 把 prompt 接口命名为 chat(),parts 直接传 list[dict],
        类型由 SDK 内部 Pydantic 校验。

        `timeout` 不传时用客户端默认(6000s);长任务可显式调大。
        `variant` 为思考强度(low/medium/high);须在 provider 模型的 variants 里登记。
        """
        kw: dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            # 当前 OpenCode POST /session/{id}/message 要 nested
            # model:{providerID,modelID}; SDK 0.1.0a36 仍发顶层 modelID,
            # additionalProperties:false 下会被丢掉,回落到 .agents/opencode.json 默认模型。
            extra: dict[str, Any] = {
                "model": {"providerID": provider_id, "modelID": model_id},
            }
            if variant:
                extra["variant"] = variant
                extra["model"]["variant"] = variant
            kw["extra_body"] = extra
            return self._client.session.chat(
                id=session_id,
                model_id=model_id,
                parts=[{"type": "text", "text": text}],
                provider_id=provider_id,
                **kw,
            )
        except APIError as e:
            raise OpencodeError(f"chat 失败:{e}") from e

    def init_session(
        self,
        session_id: str,
        message_id: str,
        provider_id: str = DEFAULT_PROVIDER,
        model_id: str = DEFAULT_MODEL,
    ) -> Any:
        """初始化 session(显式提交第一条 message id 触发 AI 回复)。

        `create_session` + `chat` 两步法在某些场景下需要先 init 才稳定。
        """
        try:
            return self._client.session.init(
                id=session_id,
                message_id=message_id,
                model_id=model_id,
                provider_id=provider_id,
            )
        except APIError as e:
            raise OpencodeError(f"init session 失败:{e}") from e


# ---- 高层便捷函数(从 VSCode 里直接 import 用) ----

def _part_attr(part: Any, name: str, default: Any = None) -> Any:
    """兼容 dict 和 object 两种 part 形态(SDK 实际是 dict)。"""
    if isinstance(part, dict):
        return part.get(name, default)
    return getattr(part, name, default)


def extract_text(message: Any) -> str:
    """从 AssistantMessage 里抽出纯文本(text + reasoning)。"""
    parts = _part_attr(message, "parts", []) or []
    out = []
    for p in parts:
        t = _part_attr(p, "type")
        if t == "text":
            out.append(_part_attr(p, "text", ""))
        elif t == "reasoning":
            r = _part_attr(p, "text", "")
            if r:
                out.append(f"[思考] {r}")
    return "\n".join(out).strip()


def extract_stats(message: Any) -> dict[str, Any]:
    """从 AssistantMessage 里抽出耗时 / token / cost 统计。

    返回 dict,字段缺失时填 None。原始时间戳是毫秒,自动转成秒。
    """
    info = _part_attr(message, "info", {}) or {}
    time_obj = info.get("time", {}) if isinstance(info, dict) else {}
    tokens = info.get("tokens", {}) if isinstance(info, dict) else {}
    cache = tokens.get("cache", {}) if isinstance(tokens, dict) else {}

    created_ms = time_obj.get("created") if isinstance(time_obj, dict) else None
    completed_ms = time_obj.get("completed") if isinstance(time_obj, dict) else None
    duration_s: float | None = None
    if isinstance(created_ms, (int, float)) and isinstance(completed_ms, (int, float)):
        duration_s = round((completed_ms - created_ms) / 1000.0, 3)

    cost = info.get("cost") if isinstance(info, dict) else None
    finish = info.get("finish") if isinstance(info, dict) else None
    model_id = info.get("modelID") if isinstance(info, dict) else None
    provider_id = info.get("providerID") if isinstance(info, dict) else None

    return {
        "duration_s": duration_s,
        "input_tokens": tokens.get("input") if isinstance(tokens, dict) else None,
        "output_tokens": tokens.get("output") if isinstance(tokens, dict) else None,
        "reasoning_tokens": tokens.get("reasoning") if isinstance(tokens, dict) else None,
        "cache_read_tokens": cache.get("read") if isinstance(cache, dict) else None,
        "cache_write_tokens": cache.get("write") if isinstance(cache, dict) else None,
        "total_tokens": tokens.get("total") if isinstance(tokens, dict) else None,
        "cost": cost,
        "finish_reason": finish,
        "model_id": model_id,
        "provider_id": provider_id,
    }


def ask(
    text: str,
    *,
    title: str = "opencode",
    session_id: str | None = None,
    provider_id: str = DEFAULT_PROVIDER,
    model_id: str = DEFAULT_MODEL,
    base_url: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """一站式:建/复用 session → 发消息 → 抽 text + stats。

    Returns: (session_id, answer_text, stats)
    stats 字段见 `extract_stats`。
    """
    client = OpencodeClient(base_url=base_url)
    sid = session_id or client.create_session(title)
    msg = client.chat(sid, text, provider_id=provider_id, model_id=model_id)
    return sid, extract_text(msg), extract_stats(msg)
