from tools.check_knowledge import package_reader, problems


def test_package_source_checks_excerpt(tmp_path):
    (tmp_path / "osis_python-0.8.1.dist-info").mkdir()
    (tmp_path / "pyosis").mkdir()
    (tmp_path / "pyosis" / "m.py").write_text('"""END = 终点张拉"""', encoding="utf-8")

    def item(qid, excerpt="END = 终点张拉"):
        return {"id": qid, "question": "q", "answer": "END", "aliases": [], "type": "api", "status": "draft",
                "source": {"package": "osis_python", "file": "pyosis/m.py", "excerpt": excerpt}}

    found = problems([item("ok"), item("gone", excerpt="BEG = 起点张拉")],
                     lambda kb, kid: None, set(), package_reader(tmp_path))
    assert found == ["gone: excerpt not found: BEG = 起点张拉"]


def test_no_answer_terms_must_be_absent_everywhere() -> None:
    def item(qid, terms):
        return {"id": qid, "question": "q", "answer": "查不到", "aliases": [], "type": "no_answer", "status": "draft",
                "absent_terms": terms}

    texts = ["mct 转 osis", "engine.solve()"]
    found = problems([item("ok", ["IFC"]), item("hit", ["MCT"]), item("bare", [])], lambda kb, kid: None, set(),
                     texts=texts)
    assert found == ["hit: 'MCT' is findable", "bare: no_answer needs absent_terms"]
