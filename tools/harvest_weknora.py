"""Cache WeKnora knowledge bases locally for writing knowledge questions (read-only API calls).

    uv run python tools/harvest_weknora.py [--out tmp/weknora] [--kb 名称 ...]

Writes <out>/<kb>/index.json (every entry: id, title, file type, updated_at) and <out>/<kb>/<id>.md (the
entry's chunks in order). The template knowledge base (``mcp.weknora.deny_kbs``) is never read.
Key: env OSIS_WEKNORA_API_KEY; base url: configs mcp.weknora.base_url.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config  # noqa: E402

TEXT_TYPES = {"md", "markdown", "txt", "pdf", "docx", "doc", "html", "py", "json", "mp4", "", None}


def client_for(cfg: dict) -> httpx.Client:
    return httpx.Client(base_url=cfg["base_url"], timeout=60,
                        headers={"X-API-Key": os.environ["OSIS_WEKNORA_API_KEY"], "User-Agent": "Mozilla/5.0"})


def entry_text(client: httpx.Client, knowledge_id: str) -> str:
    """The entry's text chunks in order."""
    chunks, page = [], 1
    while True:
        data = client.get(f"/chunks/{knowledge_id}", params={"page": page, "page_size": 100}).json()
        chunks += data.get("data") or []
        if len(chunks) >= int(data.get("total") or 0) or not data.get("data"):
            break
        page += 1
    chunks.sort(key=lambda c: c.get("chunk_index", 0))
    return "\n\n".join(c.get("content") or "" for c in chunks if c.get("chunk_type", "text") == "text")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("tmp/weknora"))
    parser.add_argument("--kb", nargs="*")
    args = parser.parse_args(argv)
    cfg = load_config()["mcp"]["weknora"]
    deny = set(cfg.get("deny_kbs") or [])
    client = client_for(cfg)
    for kb in client.get("/knowledge-bases").json()["data"]:
        if kb["name"] in deny or kb["id"] in deny or (args.kb and kb["name"] not in args.kb):
            continue
        folder = args.out / kb["name"]
        folder.mkdir(parents=True, exist_ok=True)
        entries, page = [], 1
        while True:
            data = client.get(f"/knowledge-bases/{kb['id']}/knowledge", params={"page": page, "page_size": 100}).json()
            entries += data.get("data") or []
            if len(entries) >= int(data.get("total") or 0) or not data.get("data"):
                break
            page += 1
        index = [{"id": e["id"], "title": e.get("title"), "file_type": e.get("file_type"),
                  "updated_at": e.get("updated_at")} for e in entries]
        (folder / "index.json").write_text(json.dumps({"kb": kb["name"], "kb_id": kb["id"], "kb_updated_at":
                                                       kb.get("updated_at"), "entries": index}, ensure_ascii=False,
                                                      indent=1), encoding="utf-8")
        fetched = 0
        for entry in index:
            target = folder / f"{entry['id']}.md"
            if target.exists() or entry["file_type"] not in TEXT_TYPES:
                continue
            target.write_text(f"# {entry['title']}\n\n{entry_text(client, entry['id'])}\n", encoding="utf-8")
            fetched += 1
        print(f"{kb['name']}: {len(index)} entries, fetched {fetched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
