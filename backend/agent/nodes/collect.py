"""① 증상 수집 노드 (소유자: 박근령)"""
from backend.agent.state import AgentState
from backend.agent.nodes import search
import re


def _region_confirmation(state: AgentState, region: str, near):
    """바로 앞의 지역 확인 질문에 대한 명시적 답변만 해석한다. 지역을 추측하지 않는다."""
    history = state.get("history") or []
    pending = None
    for index in range(len(history) - 1, -1, -1):
        turn = history[index]
        if turn.get("role") != "assistant":
            continue
        match = re.search(r"^‘([^’]+)’ 지역이 여러 곳이에요\. ‘([^’]+)’ 말씀이신가요\?", turn.get("content", ""))
        if match and match[1] == region:
            pending = (index, match[2])
            break
    if pending is None:
        return None
    index, candidate = pending
    confirming_now = index == len(history) - 1
    if not confirming_now and history[index + 1].get("role") != "user":
        return None
    answer = (state.get("user_input", "") if confirming_now else history[index + 1].get("content", "")).strip().strip(".!? ")
    if answer in ("네", "예", "맞아요", "맞습니다", "네 맞아요", "네 맞습니다"):
        answer = candidate
    else:
        answer = re.sub(r"^(?:네|예)[,\s]+", "", answer)
        answer = re.sub(r"\s*(?:맞아요|맞습니다|입니다|이에요|예요)$", "", answer).strip()
    found = search.find_region(answer, near=near)
    if found.get("status") != "ok":
        return None
    # 확인 발화 때문에 앞서 입력한 증상을 잃지 않도록 분류 입력으로 넘긴다.
    state["symptoms"] = [turn["content"] for i, turn in enumerate(history)
                         if i != index + 1 and turn.get("role") == "user" and isinstance(turn.get("content"), str)]
    if not confirming_now:
        state["symptoms"].append(state.get("user_input", ""))
    return found.get("name") or answer


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
    """지역명이 한 곳으로 정해지는지 확인하고 GPS는 후보 정렬에만 사용한다.
    모호하면 후보를 제안하고, 모르면 되묻는다 (FR-29·FR-30). 한 곳이면 그대로 진행.
    """
    loc = state.get("location") or {}
    has_gps = loc.get("lat") is not None and loc.get("lng") is not None
    region = loc.get("region")
    if not region:
        return

    near = (loc["lat"], loc["lng"]) if has_gps else None
    confirmed = _region_confirmation(state, region, near)
    if confirmed:
        state["location"] = {**loc, "region": confirmed}
        return
    found = search.find_region(region, near=near)
    status = found.get("status")

    if status == "ambiguous":
        candidates = found.get("candidates") or []
        if candidates:
            name = candidates[0].get("name", region)
            state["final_message"] = (
                f"‘{region}’ 지역이 여러 곳이에요. ‘{name}’ 말씀이신가요? "
                "맞으면 ‘네’ 또는 지역명으로 답해 주세요. 지역 입력란을 바꿔도 됩니다.")
        else:
            state["final_message"] = f"‘{region}’이(가) 어느 지역인지 지역 입력란에 시·군·구 또는 읍·면·동까지 적고 다시 전송해 주세요."
        state["need_more_info"] = True

    elif status == "not_found":
        state["final_message"] = f"‘{region}’ 지역을 찾지 못했어요. 지역 입력란에 시·군·구 또는 읍·면·동을 적고 다시 전송해 주세요."
        state["need_more_info"] = True

    elif status == "unavailable":
        state["final_message"] = (
            "지역 검색 정보를 지금 사용할 수 없습니다. 입력한 지역 대신 현재 위치로 임의 검색하지 않았습니다. "
            "현재 위치로 찾으려면 지역 입력란을 비우고 ‘현재 위치 사용’을 누른 뒤 다시 전송해 주세요. "
            "위치 없이 진료과 안내만 받으려면 지역 입력란을 비워 주세요.")
        state["need_more_info"] = True

    # "ok" → 확인된 지역으로 검색. unavailable은 사용자 선택 없이 GPS로 대체하지 않는다.
