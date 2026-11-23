"""T3: a fixed domain pipeline (AbaqusAgent / Foam-Agent / MCP-SIM style) in plain Python, one gateway model.

Interpreter (task -> structured spec; may decline) -> Architect (retrieval: skill files, WeKnora, pyosis API;
on a project with no code, a template from the skills copied whole as the start, as the other designs do; then an edit plan) -> Writer (full new content per file) -> Runner (deterministic: rebuild, solve, and
post/main.py when the line needs checks, all through the run's osis MCP proxy) -> Reviewer (diagnosis only)
-> Writer ... at most ``MAX_REPAIR`` rounds -> Reporter (may run read-only queries, then the final reply).
knowledge: Interpreter -> Architect (retrieval) -> Reporter, a RAG baseline. analyze: no planned edits.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from bench.osis import _SOLVE
from methods._common import base_metadata, end_turn, fail, finish, main, model_settings, normalize_tokens
from methods.mcp_cli import Session

MAX_REPAIR = 15
MAX_QUERIES = 3
FILE_CHARS, REF_CHARS = 12000, 30000
Llm = Callable[[str, str], tuple[str, dict[str, Any]]]
Mcp = Callable[[str, str, dict[str, Any]], tuple[str, bool]]

ROLES = {
    "interpreter": "你是桥梁工程任务解析员。只解析任务，不写代码，不编造数值。只输出一个 JSON 对象。",
    "architect": "你是 OSIS 建模架构师。根据检索到的资料决定改哪些文件、各改什么。只输出一个 JSON 对象。",
    "writer": "你是 OSIS pyosis 代码编写员。参数以任务解析结果为准，参考资料让位于它。只输出一个 ```python 代码块，内容是该文件的完整新内容。",
    "reviewer": "你是复核员。只诊断，不写代码。不得改动任务给定的几何、荷载、边界、材料和单位。只输出一个 JSON 对象。",
    "reporter": "你是报告员。按任务要求的格式给出最终回复。只输出一个 JSON 对象。",
}


def _json(text: str) -> dict[str, Any] | None:
    decoder, found = json.JSONDecoder(), None
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                value, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and (found is None or end - i > found[1]):
                found = (value, end - i)  # the largest object: the answer, not a nested fragment
    return found[0] if found else None


def _code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return blocks[-1] if blocks else None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit // 2] + f"\n…（省略 {len(text) - limit} 字）…\n" + text[-limit // 2:]


class Pipeline:
    def __init__(self, request: dict[str, Any], llm: Llm | None = None, mcp: Mcp | None = None) -> None:
        self.request, self.task = request, request["task"]
        self.project = Path(request["work_dir"]) if request.get("work_dir") else None
        self.skills = Path(request["skills_dir"])
        self.servers = {n: s["url"] for n, s in (request.get("mcp_servers") or {}).items()}
        self.llm, self.mcp = llm or self._chat, mcp or self._mcp
        self.model_calls, self.tool_calls, self.repairs = 0, 0, 0
        self.template: str | None = None
        self.tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        wall = float(request["timeout_s"])
        self.deadline = time.monotonic() + wall - min(120.0, wall * 0.1)  # leave time for the report

    # --- plumbing -------------------------------------------------------------------------------------------
    def _chat(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        s = model_settings(self.request)
        body: dict[str, Any] = {"model": s["model"], "seed": s["seed"],
                                "reasoning_effort": s["reasoning_effort"],
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if s["temperature"] is not None:
            body["temperature"] = s["temperature"]
        if s["max_tokens"] is not None:
            body["max_tokens"] = s["max_tokens"]
        body["stream"], body["stream_options"] = True, {"include_usage": True}  # non-streamed long replies hit gateway idle timeouts
        for attempt in range(3):
            try:
                parts: list[str] = []
                usage: dict[str, Any] = {}
                with httpx.stream("POST", s["base_url"].rstrip("/") + "/chat/completions", json=body, timeout=s["timeout"],
                                  headers={"Authorization": f"Bearer {s['api_key']}"}, trust_env=False) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        chunk = json.loads(line[5:])
                        usage = chunk.get("usage") or usage
                        for choice in chunk.get("choices") or []:
                            parts.append((choice.get("delta") or {}).get("content") or "")
                return "".join(parts), usage
            except (httpx.HTTPError, KeyError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    def _mcp(self, server: str, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        return Session(self.servers[server], float(self.request["timeout_s"])).call(tool, args)

    def ask(self, role: str, prompt: str) -> str:
        text, usage = self.llm(ROLES[role], prompt)
        self.model_calls += 1
        for key, value in normalize_tokens(usage).items():
            self.tokens[key] += value
        print(f"[{role}] #{self.model_calls} {len(prompt)}c -> {len(text)}c | {text[:160]!r}", flush=True)
        return text

    def ask_json(self, role: str, prompt: str) -> dict[str, Any]:
        for _ in range(2):
            reply = _json(self.ask(role, prompt))
            if reply is not None:
                return reply
            prompt += "\n\n上一次的输出不是合法 JSON。只输出一个 JSON 对象。"
        return {}

    def call(self, server: str, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        if server not in self.servers:
            return f"MCP server {server} is not mounted", True
        self.tool_calls += 1
        try:
            return self.mcp(server, tool, args)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}", True

    def files(self) -> dict[str, str]:
        """Project files under py/ (relative path -> text)."""
        if not self.project:
            return {}
        out = {}
        for path in sorted((self.project / "py").rglob("*.py")):
            if "__pycache__" not in path.parts and not path.name.startswith("_t3_"):
                out[path.relative_to(self.project).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        return out

    def summary(self) -> str:
        lines = []
        for rel, text in self.files().items():
            doc = re.match(r'\s*(?:"""|\'\'\')(.*?)(?:\n|"""|\'\'\')', text)
            lines.append(f"- {rel}：{doc.group(1).strip() if doc else ''}（{len(text.splitlines())} 行）")
        return "\n".join(lines) or "（没有工程文件）"

    # --- roles ----------------------------------------------------------------------------------------------
    def interpret(self) -> dict[str, Any]:
        return self.ask_json("interpreter", f"""任务：
{self.request['instructions']}

候选工程文件与模块摘要：
{self.summary()}

输出 JSON：{{"bridge_type": "...", "intent": "一句话", "parameters": {{参数名: 值}}, "missing": [简报没给、要取默认值的参数及取值],
"concerns": [简报不合理之处，没有就空], "decline": 简报不合理到应该拒做时为 true}}
没给的参数按常规默认值写进 missing 并注明，禁止编造简报里没有的具体数值当作已知。""")

    def skill_index(self) -> str:
        lines = []
        for skill in sorted(p for p in self.skills.iterdir() if (p / "SKILL.md").is_file()):
            head = (skill / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            desc = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
            files = sorted(f.relative_to(self.skills).as_posix() for f in skill.rglob("*")
                           if f.is_file() and "__pycache__" not in f.parts)
            lines.append(f"## {skill.name}：{(desc.group(1) if desc else '')[:200]}\n" + "\n".join(files[:80])
                         + (f"\n…（另有 {len(files) - 80} 个文件）" if len(files) > 80 else ""))
        return "\n".join(lines)

    def templates(self) -> list[str]:
        """Template projects in the skills (a directory with prep/main.py), relative to the skills dir."""
        return sorted(p.parent.parent.relative_to(self.skills).as_posix() for p in self.skills.rglob("prep/main.py"))

    def copy_template(self, rel: str) -> bool:
        """Copy a template whole into the empty project's py/ (only a listed one)."""
        if not self.project or self.files() or rel not in self.templates():
            return False
        shutil.copytree(self.skills / rel, self.project / "py", dirs_exist_ok=True, copy_function=shutil.copyfile,
                        ignore=shutil.ignore_patterns("__pycache__"))  # copyfile: the snapshot is read-only, the copy must not be
        self.template = rel
        return True

    def retrieve(self, spec: dict[str, Any]) -> str:
        kbs, _ = self.call("weknora", "list_knowledge_bases", {}) if "weknora" in self.servers else ("", True)
        empty = self.project is not None and not self.files()
        listing = ("\n工程里还没有代码。可整份复制为起点的模板（目录相对快照）：\n" + "\n".join(self.templates()) + "\n"
                   if empty else "")
        key = (', "template": "从模板清单里选一个最接近的模板目录，整份复制到工程作为起点，再按任务改；没有合适的留空"'
               if empty else "")
        pick = self.ask_json("architect", f"""任务：{self.request['instructions']}

解析结果：{json.dumps(spec, ensure_ascii=False)}

技能快照（目录 {self.skills}，文件路径相对它）：
{self.skill_index()}

知识库列表：{_clip(kbs, 3000) or '（无）'}
{listing}
选出要读的资料。输出 JSON：{{"skill_files": [最多 8 个文件路径], "kb_queries": [{{"kb": 库名或 id, "query": "检索词"}}，最多 3 条],
"api_queries": [pyosis API 检索的正则，最多 3 条]{key}}}""")
        if empty and pick.get("template"):
            copied = self.copy_template(str(pick["template"]))
            print(f"[architect] template {pick['template']!r}: {'copied' if copied else 'not a listed template'}", flush=True)
        parts = []
        for rel in (pick.get("skill_files") or [])[:8]:
            path = (self.skills / str(rel)).resolve()
            if path.is_file() and path.is_relative_to(self.skills.resolve()):
                parts.append(f"### 技能文件 {rel}\n{_clip(path.read_text(encoding='utf-8', errors='replace'), FILE_CHARS)}")
        for q in (pick.get("kb_queries") or [])[:3]:
            if isinstance(q, dict):
                text, _ = self.call("weknora", "hybrid_search", {"kb_id": str(q.get("kb")), "query": str(q.get("query"))})
                parts.append(f"### 知识库 {q.get('kb')}：{q.get('query')}\n{_clip(text, 4000)}")
        for pattern in (pick.get("api_queries") or [])[:3]:
            text, _ = self.call("osis", "api_grep", {"pattern": str(pattern)})
            parts.append(f"### pyosis API：{pattern}\n{_clip(text, 4000)}")
        return _clip("\n\n".join(parts), REF_CHARS)

    def plan(self, spec: dict[str, Any], refs: str) -> dict[str, Any]:
        return self.ask_json("architect", f"""任务：{self.request['instructions']}

解析结果：{json.dumps(spec, ensure_ascii=False)}

工程文件：
{self.summary()}
{f"工程是整份复制的模板 {self.template}，能直接运行：只改任务与模板不同的地方，其余文件不动。" if self.template else ""}
参考资料：
{refs}

输出 JSON：{{"references": [用到的参考], "edits": [{{"path": "py/prep/_N_xxx.py（相对工程目录）", "change": "改什么，写具体"}}]}}
{"本线是结果查询：一般不改代码，edits 留空。" if self.task == "analyze" else ""}""")

    def write(self, spec: dict[str, Any], refs: str, edits: list[dict[str, Any]], feedback: str = "") -> list[str]:
        written = []
        for edit in edits:
            rel = str(edit.get("path") or "").replace("\\", "/").lstrip("/")
            target = (self.project / rel).resolve() if self.project and rel else None
            if target is None or not target.is_relative_to((self.project / "py").resolve()) or target.suffix != ".py":
                print(f"[writer] skip {rel!r}: only .py files under py/ in the project", flush=True)
                continue
            current = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else "（新文件）"
            code = _code(self.ask("writer", f"""任务：{self.request['instructions']}

解析结果：{json.dumps(spec, ensure_ascii=False)}

要写的文件：{rel}
要做的修改：{edit.get('change')}
{f'复核意见：{feedback}' if feedback else ''}

该文件当前内容：
```python
{current}
```

参考资料：
{refs}""")) or ""
            if code.strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code, encoding="utf-8")
                written.append(rel)
        return written

    def needs_post(self) -> bool:
        brief = self.request["instructions"]
        return (self.task == "diagnose" and "验算" in brief) or "运行 py/post/main.py" in brief

    def execute(self, script: Path) -> tuple[str, bool]:
        return self.call("osis", "execute_python", {"instance_id": "solver", "file": str(script),
                                                    "cwd": str(script.parent), "timeout": int(self.request["timeout_s"])})

    def runner(self) -> str | None:
        """Rebuild, solve, then post/main.py when needed; the first failure's text, or None."""
        py = self.project / "py"
        solve = py / "_t3_solve.py"
        solve.write_text(_SOLVE.format(timeout=int(self.request["timeout_s"])), encoding="utf-8")
        steps = [("重建 prep/main.py", py / "prep" / "main.py"), ("求解", solve)]
        if self.needs_post() and (py / "post" / "main.py").is_file():
            steps.append(("组合与验算 post/main.py", py / "post" / "main.py"))
        for name, script in steps:
            text, is_error = self.execute(script)
            try:
                ok = json.loads(text).get("ok", not is_error)
            except (json.JSONDecodeError, AttributeError):
                ok = not is_error
            print(f"[runner] {name}: {'ok' if ok else 'FAILED'}", flush=True)
            if not ok:
                return f"{name} 失败：\n{_clip(text, 6000)}"
        return None

    def review(self, spec: dict[str, Any], refs: str, error: str, history: list[str]) -> dict[str, Any]:
        files = "\n\n".join(f"### {rel}\n```python\n{_clip(text, FILE_CHARS)}\n```" for rel, text in self.files().items())
        return self.ask_json("reviewer", f"""任务：{self.request['instructions']}

解析结果：{json.dumps(spec, ensure_ascii=False)}

本轮报错：
{error}

历次尝试：
{chr(10).join(history) or '（第一次）'}

当前文件：
{_clip(files, 60000)}

参考资料：
{refs}

输出 JSON：{{"root_cause": "根因", "edits": [{{"path": "相对工程目录的文件", "change": "最小改动，写具体"}}], "checklist": [核对项]}}""")

    def report(self, spec: dict[str, Any], refs: str, history: list[str]) -> str:
        notes: list[str] = []
        for _ in range(MAX_QUERIES + 1):
            can_query = self.project is not None and len(notes) < MAX_QUERIES and time.monotonic() < self.deadline
            reply = self.ask_json("reporter", f"""任务：{self.request['instructions']}

解析结果：{json.dumps(spec, ensure_ascii=False)}

过程记录：
{chr(10).join(history) or '（无）'}

参考资料：
{refs}

已做的结果查询：
{chr(10).join(notes) or '（无）'}

{f'需要从 OSIS 读结果时，可以先输出 {{"query": "只读的 pyosis 脚本（from pyosis import OSISEngine; engine = OSISEngine() ...，print 要的内容）"}}，最多 {MAX_QUERIES} 次。' if can_query else ''}
否则输出 {{"final": "最终回复全文，按任务要求的格式"}}。""")
            if can_query and reply.get("query") and not reply.get("final"):
                script = self.project / "py" / f"_t3_query{len(notes) + 1}.py"
                script.write_text(str(reply["query"]), encoding="utf-8")
                text, _ = self.execute(script)
                notes.append(f"查询 {len(notes) + 1}：\n{reply['query']}\n输出：\n{_clip(text, 6000)}")
                continue
            # models often answer in the task's own JSON ({"value": ..}, {"localizations": ..}) without the "final" wrapper
            return str(reply.get("final") or (json.dumps(reply, ensure_ascii=False) if reply and not reply.get("query") else ""))
        return ""

    # --- flow -----------------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        spec = self.interpret()
        history: list[str] = [f"解析：{json.dumps(spec, ensure_ascii=False)[:2000]}"]
        if spec.get("decline"):
            history.append("解析认为简报不合理，拒做：" + "；".join(map(str, spec.get("concerns") or [])))
            return self.finish(spec, "", history)
        refs = self.retrieve(spec)
        if self.project is None:  # knowledge: retrieval then answer
            return self.finish(spec, refs, history)
        if self.template:
            history.append(f"起点：复制模板 {self.template}")
        plan = self.plan(spec, refs)
        written = self.write(spec, refs, [e for e in plan.get("edits") or [] if isinstance(e, dict)])
        history.append(f"计划：{json.dumps(plan, ensure_ascii=False)[:2000]}；写入 {written}")
        while True:
            error = self.runner()
            if error is None:
                history.append("运行通过（重建、求解" + ("、验算" if self.needs_post() else "") + "）")
                break
            history.append(f"第 {self.repairs + 1} 次运行失败：{error[:1500]}")
            if self.repairs >= MAX_REPAIR or time.monotonic() > self.deadline:
                history.append("修复轮数或时间用完，停止")
                break
            self.repairs += 1
            diagnosis = self.review(spec, refs, error, history)
            edits = [e for e in diagnosis.get("edits") or [] if isinstance(e, dict)]
            written = self.write(spec, refs, edits, json.dumps(diagnosis, ensure_ascii=False))
            history.append(f"复核：{str(diagnosis.get('root_cause'))[:800]}；改了 {written}")
            if not written:  # rerunning unchanged files would fail the same way
                history.append("复核没有给出可写的改动，停止")
                break
        return self.finish(spec, refs, history)

    def finish(self, spec: dict[str, Any], refs: str, history: list[str]) -> dict[str, Any]:
        final = self.report(spec, refs, history)
        return {"final_answer": final, "model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "tokens": self.tokens, "repair_rounds": self.repairs, "declined": bool(spec.get("decline"))}


def turns(request: dict[str, Any], llm: Llm | None, mcp: Mcp | None) -> dict[str, Any]:
    """project: no session to continue, so each turn is a fresh pipeline on the current project, given the earlier
    turns' final replies; each gets the time the earlier ones left."""
    deadline = time.monotonic() + float(request["timeout_s"])
    replies: list[str] = []
    total: dict[str, Any] = {"model_calls": 0, "tool_calls": 0, "repair_rounds": 0,
                             "tokens": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    for text in [request["instructions"], *request["turns"]]:
        if replies:
            text += "\n\n前几轮（同一工程）的最终回复：\n" + "\n\n".join(
                f"第 {k} 轮：\n{_clip(r, 4000)}" for k, r in enumerate(replies, start=1))
        out = Pipeline({**request, "instructions": text, "timeout_s": deadline - time.monotonic()}, llm, mcp).run()
        for key in ("model_calls", "tool_calls", "repair_rounds"):
            total[key] += out[key]
        for key, value in out["tokens"].items():
            total["tokens"][key] += value
        total["final_answer"] = out["final_answer"]
        if not str(out["final_answer"]).strip():
            break
        replies.append(out["final_answer"])
        end_turn(request, out["final_answer"])
    return total


def run(request: dict[str, Any], *, llm: Llm | None = None, mcp: Mcp | None = None) -> dict[str, Any]:
    started = time.monotonic()
    metadata = base_metadata(request, framework="python-pipeline", interaction_mode="fixed_pipeline",
                             roles=list(ROLES), max_repair=MAX_REPAIR)
    try:
        metadata.update(turns(request, llm, mcp) if "turns" in request else Pipeline(request, llm, mcp).run())
        if not str(metadata.get("final_answer") or "").strip():
            raise RuntimeError("the reporter gave no final answer")
        metadata.update(status="completed", stop_reason="completed")
    except Exception as exc:  # noqa: BLE001
        fail(metadata, exc)
    return finish(request, metadata, started)


if __name__ == "__main__":
    raise SystemExit(main(run))
