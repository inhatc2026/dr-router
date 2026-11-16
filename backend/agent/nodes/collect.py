"""① 증상 수집 노드 (소유자: 박근령)"""
from backend.agent.state import AgentState
from backend.agent.nodes import search


def collect_symptoms(state: AgentState) -> AgentState:
    """자연어 입력에서 핵심 증상을 추출하고, 지역명(GPS 거부 시)을 확인한다.
    정보가 부족하면 state["need_more_info"] = True 로 두어 되물음 루프를 돈다.
    (Memory: state["history"] 활용)
    """
    # TODO: LLM으로 증상 추출 + 충분성 판단 (현재는 스텁)
    state.setdefault("symptoms", [])
    state["need_more_info"] = False

    _resolve_region(state)
    return state


def _resolve_region(state: AgentState) -> None:
    """GPS가 없고 지역명만 있을 때, 지역명이 한 곳으로 정해지는지 확인한다.
    모호하면 후보를 제안하고, 모르면 되묻는다 (FR-29·FR-30). 한 곳이면 그대로 진행.
    """
    loc = state.get("location") or {}
    has_gps = loc.get("lat") is not None and loc.get("lng") is not None
    region = loc.get("region")
    if not region or has_gps:        # GPS가 있으면 검색 쪽에서 알아서 처리
        return

    found = search.find_region(region)   # near=None (GPS 없음)
    status = found.get("status")

    if status == "ambiguous":
        candidates = found.get("candidates") or []
        if candidates:
            name = candidates[0].get("name", region)
            state["final_message"] = (
                f"‘{region}’ 지역이 여러 곳이에요. ‘{name}’ 말씀이신가요? "
                "맞으면 그 이름으로 다시 입력해 주세요.")
        else:
            state["final_message"] = f"‘{region}’이(가) 어느 지역인지 시·군·구까지 알려주시겠어요?"
        state["need_more_info"] = True

    elif status == "not_found":
        state["final_message"] = f"‘{region}’ 지역을 찾지 못했어요. 시·군·구 이름으로 다시 알려주시겠어요?"
        state["need_more_info"] = True

    # "ok" / "unavailable" → 그대로 진행 (검색에서 region 또는 GPS 사용)
