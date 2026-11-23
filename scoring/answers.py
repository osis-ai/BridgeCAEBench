"""GAIA 风格问答打分：抽出 FINAL ANSWER 后做准精确匹配。复制自官方 datasets/qa/score.py（commit 37f7685）。"""

from __future__ import annotations

import re
import string
import unicodedata
from typing import Any


# 不用 DOTALL：`.` 不跨行，findall 才能拿到每一次 FINAL ANSWER，再取最后一次。
_FINAL = re.compile(r"FINAL ANSWER:\s*(.+)", re.IGNORECASE)
_THINK_TAG = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)


def extract_final_answer(text: str) -> str:
    if not text:
        return ""
    clean = _THINK_TAG.sub("", text)
    matches = _FINAL.findall(clean)
    if matches:
        return matches[-1].strip().splitlines()[0].strip()
    return clean.strip().splitlines()[-1].strip()


def _normalize_number(s: str) -> str:
    return s.replace("$", "").replace("%", "").replace(",", "").strip()


def _normalize_str(s: str, *, remove_punct: bool = True) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", "", s)
    if remove_punct:  # string.punctuation is ASCII only: also drop Chinese punctuation (。、“”「」…)
        s = "".join(ch for ch in s if ch not in string.punctuation and not unicodedata.category(ch).startswith("P"))
    return s


def _is_float(s: str) -> bool:
    try:
        float(_normalize_number(s))
        return True
    except (TypeError, ValueError):
        return False


def _norm_api(s: str) -> str:
    s = s.strip().rstrip("()")
    s = s.replace("OSISEngine()", "engine")
    s = s.replace(" ", "")
    return s


def api_equivalent(pred: str, gold: str) -> bool:
    p = _norm_api(pred)
    g = _norm_api(gold)
    if p == g:
        return True
    if p.split(".")[-1] != g.split(".")[-1]:
        return False
    path = [tok for tok in g.split(".") if tok and tok != "engine"][:-1]
    pl = p.lower()
    return bool(path) and all(tok.lower() in pl for tok in path)


def _yes_no_aliases(gold: str) -> list[str]:
    g = gold.strip()
    if g in {"是", "yes", "Yes", "true", "True"}:
        return ["是", "yes", "true"]
    if g in {"否", "false", "False"}:
        return ["否", "no", "false", "不存在"]
    return []


_LIST_SEP = r"[,;，；、]"


def _split_list(s: str) -> list[str]:
    return [elem.strip() for elem in re.split(_LIST_SEP, s) if elem.strip()]


_PAREN = re.compile(r"^(.*?)[\(（]([^）)]+)[\)）]\s*$")


def _paren_variants(s: str) -> list[str]:
    """「可变参数（varargs）」同时接受整串、括号外、括号内。"""
    s = (s or "").strip()
    if not s:
        return []
    out = [s]
    m = _PAREN.fullmatch(s)
    if m:
        outer, inner = m.group(1).strip(), m.group(2).strip()
        if outer and outer not in out:
            out.append(outer)
        if inner and inner not in out:
            out.append(inner)
    return out


def _match_atomic_one(pred: str, gold: str) -> bool:
    if _is_float(gold):
        try:
            return float(_normalize_number(pred)) == float(_normalize_number(gold))
        except ValueError:
            return False
    if _normalize_str(pred) == _normalize_str(gold):
        return True
    if "." in gold and api_equivalent(pred, gold):
        return True
    return False


def _match_atomic(pred: str, gold: str) -> bool:
    return any(_match_atomic_one(p, gold) for p in _paren_variants(pred))


def match_answer(pred: str, gold: str, aliases: list[str] | None = None) -> bool:
    """与 GAIA question_scorer 同类：数字比大小，逗号/分号列表逐元比较，否则规范化字符串。

    另加 aliases、接口路径等价、括号注释（「可变参数（varargs）」可对 gold 或 alias）。
    金标准为参数名 ``no`` 时不把中文「否」当正确。
    """
    if pred is None or gold is None:
        return False
    pred = str(pred).strip()
    gold = str(gold).strip()
    candidates = [gold, *(aliases or []), *_yes_no_aliases(gold)]

    for c in candidates:
        c = str(c).strip()
        if re.search(_LIST_SEP, c) and not _is_float(c):
            gt_elems = _split_list(c)
            ma_elems = _split_list(pred)
            if len(gt_elems) == len(ma_elems) and all(
                _match_atomic(m, g) for m, g in zip(ma_elems, gt_elems)
            ):
                return True
            continue
        if _match_atomic(pred, c):
            return True
    return False


def score_item(model_text: str, item: dict[str, Any]) -> bool:
    pred = extract_final_answer(model_text)
    gold = item.get("Final answer", item.get("final_answer", ""))
    aliases = item.get("aliases") or []
    return match_answer(pred, gold, aliases)
