"""knowledge: short-answer questions about OSIS, answered from the skills, pyosis API docs and WeKnora.

Scored on the reply's last ``FINAL ANSWER:`` line by a small model judging whether it means the gold answer
(``scoring.judge``, ``tasks.knowledge.judge`` in the config); when the model cannot give a verdict, or no judge is
configured, by answer match (``scoring.answers``: aliases, number normalization, API equivalence).
A no-answer question is right when the reply says it cannot be found (its aliases). On any other question such a reply
is wrong by code, before the model is asked: the judge once took "查不到" for right on questions that have an answer.
A video question (``video_url``) also needs the video's link somewhere in the reply, matched on its BV id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scoring import answers, cases, judge
from tasks import _project as p

SERIAL = False
FOLDER = cases.DATASETS / "knowledge"
CANNOT_FIND = ("查不到", "未找到", "无法确定", "不确定", "知识库中没有")  # the aliases of the no-answer questions


def says_cannot_find(answer: str) -> bool:
    return answers._normalize_str(answer).startswith(CANNOT_FIND)


def load_items(ctx: Any) -> list[dict[str, Any]]:
    items = [q for f in sorted(FOLDER.glob("*.yaml")) for q in yaml.safe_load(f.read_text(encoding="utf-8"))
             if q["status"] in ("verified", "frozen")]
    ids = [q["id"] for q in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate knowledge id")
    return items


item_id = p.item_id


def hidden(ctx: Any) -> list[str]:
    return []


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    system = (FOLDER / "system_prompt.txt").read_text(encoding="utf-8").strip()
    # the link goes on its own line: digits in a URL would spoil number matching on the FINAL ANSWER line
    video = (" The answer comes from an OSIS tutorial video: put that video's URL on a line `VIDEO: <url>` "
             "just before the FINAL ANSWER line." if item.get("video_url") else "")
    return {"instructions": (f"{system}\n\nAnswer the following OSIS question using only the mounted skills and the "
                             "provided tools. Do not look for datasets, evaluator files, other runs, or answers. "
                             f"End with exactly one FINAL ANSWER line.{video}\n\nQuestion:\n{item['question'].strip()}"),
            "work_dir": "", "writable": False, "deliverable": "answer",
            "native": {"item_id": item["id"], "question": item["question"]}}


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    record = p.base_record(ctx, {**item, "line": "knowledge", "kind": item.get("type")}, run_dir, generation)
    raw = str(generation.get("final_answer") or "")
    aliases = item.get("aliases") or []
    record["answer"] = answers.extract_final_answer(raw)
    record["rule_correct"] = not record["error"] and answers.score_item(
        raw, {"final_answer": item["answer"], "aliases": aliases})
    record["judge"] = None
    cfg = (ctx.task_cfg or {}).get("judge")
    # the model decides, the rule only when it cannot; "cannot be found" on a question with an answer is wrong outright
    record["cannot_find"] = item.get("type") != "no_answer" and says_cannot_find(record["answer"])
    if cfg and not record["error"] and record["answer"].strip() and not record["cannot_find"]:
        record["judge"] = judge.judge(cfg, item["question"], str(item["answer"]), [str(a) for a in aliases],
                                      record["answer"])
    record["correct"] = float(record["judge"]["correct"] if record["judge"] else record["rule_correct"])
    if item.get("video_url"):
        record["video_url_ok"] = item["video_url"].rsplit("/", 1)[-1] in raw
        record["correct"] *= record["video_url_ok"]
    record.update(p.grade("knowledge", record))
    return record


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    return p.summarize("knowledge", records)
