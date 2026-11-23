from __future__ import annotations

import pytest

from bench.weknora_mcp import Refused, guard

KBS = [{"id": "11111111-1111-1111-1111-111111111111", "name": "说明书"},
       {"id": "22222222-2222-2222-2222-222222222222", "name": "模板库"}]
ENTRY_OK, ENTRY_BAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def fake(method: str, endpoint: str, **kwargs: object) -> dict:
    if endpoint == "/knowledge-bases":
        return {"data": list(KBS), "success": True}
    if endpoint.startswith("/knowledge/"):
        return {"data": {"knowledge_base_id": KBS[0 if endpoint.endswith(ENTRY_OK) else 1]["id"]}}
    return {"data": [], "endpoint": endpoint}


def test_guard_hides_and_refuses_the_denied_knowledge_base() -> None:
    request = guard(fake, ["模板库"])
    assert [kb["name"] for kb in request("GET", "/knowledge-bases")["data"]] == ["说明书"]
    assert request("POST", f"/knowledge-bases/{KBS[0]['id']}/hybrid-search", json={"query": "x"})["data"] == []
    assert request("GET", f"/chunks/{ENTRY_OK}")["endpoint"] == f"/chunks/{ENTRY_OK}"
    for endpoint, kwargs in ((f"/knowledge-bases/{KBS[1]['id']}/hybrid-search", {}),
                             (f"/knowledgebase/{KBS[1]['id']}/wiki/search", {}),
                             (f"/knowledge/{ENTRY_BAD}", {}), (f"/chunks/{ENTRY_BAD}", {}),
                             ("/knowledge/search", {"params": {"kb_ids": KBS[1]["id"]}})):
        with pytest.raises(Refused):
            request("GET", endpoint, **kwargs)


def test_guard_fails_closed_on_an_unknown_deny_name() -> None:
    with pytest.raises(SystemExit):
        guard(fake, ["不存在的库"])
