"""逐运行导出（每个 LLM × 设计 × 题一行），供论文侧画图、算置信区间。
用法（在仓库根目录下）：uv run python tools/export_runs.py <输出 csv>
只导已完成（state=done）的格子；网关 400 拒收的格子不导（待重跑），与 make_csv.py 一致。"""
import csv, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.config import load_config, resolve  # noqa: E402
from tasks._project import behaviour  # noqa: E402  从 mcp_calls 重算：旧记录的 osis_errors 漏了脚本异常

RUNS = resolve(load_config()["runs_root"])
LLM = {"deepseek": "deepseek-v4.1-flash", "gpt": "gpt-6.1-sol", "minimax": "minimax-m3.1-flash"}
KNOWLEDGE = {"deepseek": {"T1": "knowledge-deepseek", "T2": "knowledge-v41", "T3": "knowledge-v41", "T5": "knowledge-v41-t5"}}
LINES = ["knowledge", "build", "change", "handover", "diagnose", "analyze", "project"]
FIELDS = ["llm", "design", "line", "case_id", "bridge_type", "kind", "status", "solved", "C", "P", "M", "L", "R", "A",
          "answer", "pushback", "turns_done", "first_failed_turn", "quality", "efficiency", "cost", "total",
          "tokens_in", "tokens_cached", "tokens_out", "cost_usd", "duration_s", "wall_s", "n_osis_calls",
          "n_osis_errors", "recovered", "ran_main", "script_newer_than_output", "unextracted_checks",
          "reviewer_rounds", "reopen_rounds", "deferred", "kb_calls", "judge_model", "rule_correct", "error"]


def status(r, g):
    err = str(r.get("error") or "")
    if not err:
        return "ok"
    if "Timeout" in err:
        return "timeout"
    if "without a submission" in err or "no final answer" in err.lower():
        return "no_submission"
    return "crash"


def row(tag, design, line, r, g, run_dir):
    u, b, mcp = r.get("usage") or {}, behaviour(run_dir), g.get("mcp") or {}
    checks = ((r.get("detail") or {}).get("checks") or {}).get("results") or []
    failed = bool(r.get("error"))
    z = lambda k: 0.0 if failed else r.get(k)  # 运行出错的题各分记 0（评分规则）
    return {"llm": LLM[tag], "design": design, "line": line, "case_id": r["item_id"], "bridge_type": r.get("bridge"),
            "kind": r.get("kind"), "status": status(r, g), "solved": r.get("solved"),
            "C": r.get("checks"), "P": r.get("preserve"), "M": r.get("conformance"), "L": r.get("line_localize"),
            "R": r.get("line_repair"), "A": r.get("line_assume"),
            "answer": r.get("line_answer") if line == "analyze" else r.get("correct"),
            "pushback": 0.0 if failed and r.get("kind") == "infeasible" else r.get("line_pushback"),
            "turns_done": r.get("turns_done"), "first_failed_turn": r.get("first_failed_turn"),
            "quality": z("score"), "efficiency": z("efficiency"), "cost": z("cost"), "total": z("total"),
            "tokens_in": u.get("prompt_tokens"), "tokens_cached": u.get("cached_tokens"), "tokens_out": u.get("completion_tokens"),
            "cost_usd": r.get("cost_usd"), "duration_s": r.get("duration_s"), "wall_s": r.get("latency_s"),
            "n_osis_calls": b.get("osis_runs"), "n_osis_errors": b.get("osis_errors"),
            "recovered": (r.get("solved") == 1.0) if b.get("osis_errors") and r.get("solved") is not None else None,
            "ran_main": b.get("ran_main"), "script_newer_than_output": (r.get("state") or {}).get("code_after_solve"),
            "unextracted_checks": sum(bool(c.get("unextracted")) for c in checks) if checks else None,
            "reviewer_rounds": g.get("repair_rounds"),
            "reopen_rounds": g.get("reopened"), "deferred": g.get("deferred"),
            "kb_calls": (mcp.get("weknora") or {}).get("calls"), "judge_model": (r.get("judge") or {}).get("model"),
            "rule_correct": r.get("rule_correct"), "error": str(r.get("error") or "")[:120]}


rows = []
for tag in LLM:
    for line in LINES:
        for design in ("T1", "T2", "T3", "T4", "T5"):
            label = KNOWLEDGE.get(tag, {}).get(design, f"full-{line}-{tag}") if line == "knowledge" else f"full-{line}-{tag}"
            camp = RUNS / label / "campaign.json"
            cells = {(c[0], c[1]) for c in json.loads(camp.read_text(encoding="utf-8"))["cells"]} if camp.is_file() else set()
            for s in sorted((RUNS / label / design).glob("*/seed0/status.json")):
                rec = s.with_name("record.json")
                if (design, s.parent.parent.name) not in cells:
                    continue  # 不在当前 campaign 里的格子（如 11-03 试跑后撤下的 T3 build）
                if json.loads(s.read_text(encoding="utf-8")).get("state") != "done" or not rec.is_file():
                    continue
                r = json.loads(rec.read_text(encoding="utf-8"))
                if (r.get("usage") or {}).get("last_status") == 400 and r.get("generation_status") == "failed":
                    continue
                gen = s.with_name("generation.json")
                g = json.loads(gen.read_text(encoding="utf-8")) if gen.is_file() else {}
                rows.append(row(tag, design, line, r, g, s.parent))
assert len({(x["llm"], x["design"], x["line"], x["case_id"]) for x in rows}) == len(rows), "一格一行"
with open(sys.argv[1], "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, FIELDS)
    w.writeheader()
    w.writerows(rows)
print(len(rows), "rows ->", sys.argv[1])
