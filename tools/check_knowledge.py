"""Check knowledge questions: fields, unique ids, and that each excerpt is still in its WeKnora entry.

    uv run python tools/check_knowledge.py [--cache tmp/weknora] [文件 ...]

Without ``--cache`` the entries are read live (env OSIS_WEKNORA_API_KEY, read-only); with it, from the
``tools/harvest_weknora.py`` cache. A question citing a denied knowledge base (``deny_kbs``) is an error.
pyosis questions cite the package installed in the OSIS-AI venv instead (``source: {package, file}``);
the excerpt must still be in the installed file. No-answer questions list ``absent_terms`` that must appear
nowhere: not in the cached allowed knowledge bases, the installed pyosis, or the mounted skills.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring.cases import DATASETS, STATUSES  # noqa: E402

REQUIRED = ("id", "question", "answer", "aliases", "type", "status")


def normalize(text: str) -> str:
    """Compare text the way it reads: no quote/heading marks, escapes, emphasis or whitespace; entities decoded."""
    text = re.sub(r"(?m)^[>#\s]+", "", html.unescape(text))
    return re.sub(r"\s+|\*", "", re.sub(r"\\(.)", r"\1", text))


def package_reader(site: Path):
    """``(package, file)`` -> the file's text, or an error string starting with '!'."""
    def text_of(package: str, file: str) -> str:
        if not any(site.glob(f"{package}-*.dist-info")):
            return f"!{package} not installed in {site}"
        path = site / file
        return path.read_text(encoding="utf-8") if path.is_file() else f"!{file} not in {site}"
    return text_of


def corpus(cache: Path, deny: set[str], *roots: Path) -> list[str]:
    """Lower-cased text of every cached entry of an allowed kb, and of every text file under ``roots``."""
    files = [f for kb in cache.iterdir() if kb.is_dir() and kb.name not in deny for f in kb.glob("*.md")]
    files += [f for root in roots for f in root.rglob("*") if f.suffix in {".md", ".py", ".yaml", ".txt", ".json"}]
    return [f.read_text(encoding="utf-8", errors="replace").lower() for f in files]


def problems(items: list[dict], text_of, deny: set[str], package_text=None, texts: list[str] | None = None) -> list[str]:
    """``text_of(kb, knowledge_id)`` returns the entry's text, or None if the entry is not in that kb."""
    out, seen = [], set()
    for item in items:
        qid = item.get("id", "?")
        out += [f"{qid}: missing {k}" for k in REQUIRED if item.get(k) in (None, "")]
        if qid in seen:
            out.append(f"{qid}: duplicate id")
        seen.add(qid)
        if item.get("status") not in STATUSES:
            out.append(f"{qid}: bad status {item.get('status')!r}")
        sources = item.get("source") or []
        sources = sources if isinstance(sources, list) else [sources]  # multi-hop questions cite several entries
        if item.get("type") == "no_answer":
            terms = item.get("absent_terms") or []
            if not terms:
                out.append(f"{qid}: no_answer needs absent_terms")
            elif texts is None:
                out.append(f"{qid}: no_answer is checked against the cache only (--cache)")
            out += [f"{qid}: '{t}' is findable" for t in terms if texts and any(t.lower() in x for x in texts)]
            continue
        if any("skill" in s for s in sources):
            continue
        if not sources:
            out.append(f"{qid}: missing source")
        for source in sources:
            if "package" in source:
                text = package_text(source["package"], source.get("file", ""))
                if text.startswith("!"):
                    out.append(f"{qid}: {text[1:]}")
                elif normalize(source.get("excerpt", "")) not in normalize(text) or not source.get("excerpt"):
                    out.append(f"{qid}: excerpt not found: {source.get('excerpt')}")
                continue
            if source.get("kb") in deny:
                out.append(f"{qid}: cites denied kb {source['kb']}")
                continue
            if not all(source.get(k) for k in ("kb", "knowledge_id", "excerpt")):
                out.append(f"{qid}: source needs kb, knowledge_id, excerpt")
                continue
            text = text_of(source["kb"], source["knowledge_id"])
            if text is None:
                out.append(f"{qid}: {source['knowledge_id']} not in {source['kb']}")
            elif (normalize(source["excerpt"]) in normalize(text)) == bool(source.get("absent")):
                out.append(f"{qid}: excerpt {'present' if source.get('absent') else 'not found'}: {source['excerpt']}")
    return out


def cache_reader(folder: Path):
    def text_of(kb: str, knowledge_id: str) -> str | None:
        path = folder / kb / f"{knowledge_id}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None
    return text_of


def live_reader(cfg: dict):
    from tools.harvest_weknora import client_for, entry_text
    client = client_for(cfg)
    kb_ids = {kb["name"]: kb["id"] for kb in client.get("/knowledge-bases").json()["data"]}
    cache: dict[str, str | None] = {}

    def text_of(kb: str, knowledge_id: str) -> str | None:
        if knowledge_id not in cache:
            entry = client.get(f"/knowledge/{knowledge_id}").json().get("data") or {}
            owned = kb in kb_ids and entry.get("knowledge_base_id") == kb_ids[kb]
            cache[knowledge_id] = entry_text(client, knowledge_id) if owned else None
        return cache[knowledge_id]
    return text_of


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    cfg = load_config()["mcp"]["weknora"]
    files = args.files or sorted((DATASETS / "knowledge").glob("*.yaml"))
    items = [item for f in files for item in yaml.safe_load(f.read_text(encoding="utf-8"))]
    text_of = cache_reader(args.cache) if args.cache else live_reader(cfg)
    site = Path(cfg["python"]).expanduser().parents[1] / "Lib" / "site-packages"  # the OSIS-AI venv
    deny = set(cfg.get("deny_kbs") or [])
    skills = resolve(load_config()["parent_repo"]) / "skills"
    texts = corpus(args.cache, deny, site / "pyosis", skills) if args.cache else None  # shortcut: no live corpus scan
    found = problems(items, text_of, deny, package_reader(site), texts)
    print("\n".join(found) or f"{len(items)} questions ok ({len(files)} files)")
    print(json.dumps({t: sum(i.get("type") == t for i in items) for t in sorted({i.get("type") for i in items})},
                     ensure_ascii=False))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
