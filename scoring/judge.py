"""AI 评分：小模型给一个对 / 错判定。走本机命令行：`pi -p` 或 `claude -p`。

``judge``：问答回答与标准答案是不是同一个意思，按（提示词版本、题目、标准答案、别名、回答）缓存在
datasets/knowledge/judged/verdicts.jsonl。``verdict``：工程类回复的文字部分（是否指出需求不合理、是否说明假定值），
按（判分说明、各项输入）缓存在调用方给的文件。判定不分判分模型（视为等价，行里记着是哪个模型判的），
同一输入只判一次，可复查、可复现。命令不可用、超时或输出不合格时返回 None，调用方退回规则判分。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scoring import cases

VERDICTS = cases.DATASETS / "knowledge" / "judged" / "verdicts.jsonl"
VERSION = 2  # bump when RULES change: old verdicts no longer apply
ROLE = "你是问答题评分员，只输出 JSON。"  # one line: npm .cmd shims mangle multi-line arguments, so the rules go on stdin
RULES = """判断待评回答是否答对 OSIS 桥梁软件的问答题。
- 意思与标准答案相同就算对：措辞、标点、语序、单位写法、菜单路径分隔符不同都不影响；多出的补充说明只要不与标准答案矛盾也算对。
- 缺少标准答案的关键信息、给出不同的值或结论、或同时给出互相矛盾的多个答案，算错。
- 标准答案是"查不到"的题：只有回答明确表示查不到、找不到依据或无法确定才算对；给出肯定或否定的结论（如"是""否""不能""不支持"）都算错。
只输出一行 JSON：{"correct": true 或 false, "reason": "一句话理由"}"""
SCHEMA = {"type": "object", "properties": {"correct": {"type": "boolean"}, "reason": {"type": "string"}},
          "required": ["correct", "reason"]}
OUTPUT = '只输出一行 JSON：{"correct": true 或 false, "reason": "一句话理由"}'
# each CLI with every tool, plugin and context file off: the judge only reads the prompt
ARGS = {
    "claude": lambda model, role: ["-p", "--model", model, "--tools", "", "--no-session-persistence",
                                   "--system-prompt", role, "--output-format", "json", "--json-schema", json.dumps(SCHEMA)],
    "pi": lambda model, role: ["-p", "--model", model, "--no-session", "--no-tools", "--no-extensions", "--no-skills",
                               "--no-context-files", "--no-prompt-templates", "--no-mcp", "--offline",
                               "--system-prompt", role],
}


def _key(question: str, gold: str, aliases: list[str], answer: str) -> str:
    """Judge models count as interchangeable: a verdict from any of them is reused (the row records which)."""
    raw = json.dumps([VERSION, question.strip(), gold, aliases, answer], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cached(store: Path) -> dict[str, dict[str, Any]]:
    if not store.exists():
        return {}
    rows = (json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line.strip())
    return {r["key"]: r for r in rows}


def _parse(cli: str, stdout: str) -> dict[str, Any]:
    if cli == "claude":
        return json.loads(stdout)["structured_output"]
    found = [o for o in re.findall(r"\{[^{}]*\}", stdout) if '"correct"' in o]
    return json.loads(found[-1])


def _ask(cfg: dict[str, Any], store: Path, key: str, role: str, prompt: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if key in (hit := _cached(store)):
        return {k: hit[key][k] for k in ("correct", "reason", "model")}
    cli, model = cfg.get("cli", "pi"), cfg["model"]
    try:  # a neutral cwd keeps project context files away from the judge
        done = subprocess.run([shutil.which(cli) or cli, *ARGS[cli](model, role)], input=prompt, capture_output=True,
                              text=True, encoding="utf-8", timeout=cfg.get("timeout_s", 120), cwd=tempfile.gettempdir())
        verdict = _parse(cli, done.stdout)
        verdict = {"correct": verdict["correct"], "reason": str(verdict["reason"]), "model": model}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        return None
    if not isinstance(verdict["correct"], bool):  # "false" as a string must not count as true
        return None
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, **verdict, **row}, ensure_ascii=False) + "\n")
    return verdict


def judge(cfg: dict[str, Any], question: str, gold: str, aliases: list[str], answer: str) -> dict[str, Any] | None:
    """{"correct", "reason", "model"}, or None when the judge could not give a verdict."""
    prompt = (f"{RULES}\n\n题目：{question.strip()}\n标准答案：{gold}\n可接受的别名：{'、'.join(aliases) or '无'}\n"
              f"待评回答：{answer}")
    return _ask(cfg, VERDICTS, _key(question, gold, aliases, answer), ROLE, prompt,
                {"question": question.strip(), "gold": gold, "aliases": aliases, "answer": answer})


def verdict(cfg: dict[str, Any], store: Path, rules: str, inputs: dict[str, str]) -> dict[str, Any] | None:
    """One yes/no on an engineering reply: ``rules`` says what counts as yes, ``inputs`` are labelled texts."""
    key = hashlib.sha256(json.dumps([rules, inputs], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    prompt = "\n\n".join([rules, OUTPUT, *(f"{name}：\n{text}" for name, text in inputs.items())])
    return _ask(cfg, store, key, "你是桥梁建模任务的评分员，只输出 JSON。", prompt, {"rules": rules, **inputs})
