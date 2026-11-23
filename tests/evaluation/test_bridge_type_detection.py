"""目录名桥型路由：未正式支持的现浇/变截面箱梁保持 unknown。"""
from scoring.evaluation.levels.model_conformance import detect_bridge_type


def test_unsupported_cast_in_place_names_remain_unknown() -> None:
    assert detect_bridge_type("现浇等截面箱梁-25+25") == "unknown"
    assert detect_bridge_type("现浇变宽变截面箱梁-40+60+40") == "unknown"
    assert detect_bridge_type("变截面连续箱梁-40+60+40") == "unknown"


def test_explicit_cantilever_name_still_routes_to_cantilever() -> None:
    assert detect_bridge_type("变截面悬浇连续梁-40+70+40") == "cantilever_box"
