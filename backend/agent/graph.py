"""
LangGraph 그래프 정의 (소유자: 박근령)

흐름:
  START → collect ─(정보 충분?)─ 아니오 → ask(되묻기) → END
                     │ 예
                     ▼
                   triage ─(응급?)─ 예 → emergency(119 + 응급실) → END
                     │ 아니오
                     ▼
                  classify → search → respond → END

- 응급 분기: search.search_emergency_rooms 로 가까운 응급실을 hospitals에 채운다 (최건, FR-08·IF-03)
- region : run_agent에서 ChatRequest.region → location["region"] 로 넘기고,
           검색 쪽(search.resolve_location)이 지역명을 GPS보다 우선 사용한다 (EX-03)
           지역명이 모호/미확인이면 collect에서 되묻는다 (FR-29·FR-30)
"""
from langgraph.graph import StateGraph, START, END

from backend.api.schemas import ChatRequest, ChatResponse, DepartmentOut, HospitalOut
from backend.agent.state import AgentState
from backend.agent.nodes import collect, triage, classify, search


# ── 분기(라우터) 함수: 반환 문자열이 다음 노드 이름 ──
def route_after_collect(state: AgentState) -> str:
    return "ask" if state.get("need_more_info") else "triage"


def route_after_triage(state: AgentState) -> str:
    return "emergency" if state.get("emergency") else "classify"


# ── 메시지를 작성하는 터미널 노드들 ──
def ask_node(state: AgentState) -> dict:
    """정보 부족/지역 되묻기 — collect가 넣어둔 질문이 있으면 그걸 쓴다."""
    msg = state.get("final_message") or "증상을 조금 더 자세히 알려주시겠어요? (언제부터 · 어디가 · 어떻게)"
    return {"final_message": msg, "need_more_info": True}


def emergency_node(state: AgentState) -> dict:
    """응급: 119 안내 + 가까운 응급실을 hospitals에 채운다."""
    found = search.search_emergency_rooms(state)   # {"hospitals": [...]}
    return {
        "final_message": "응급 증상이 의심됩니다. 즉시 119에 연락하거나 가까운 응급실을 방문하세요.",
        **found,
    }


def respond_node(state: AgentState) -> dict:
    depts = state.get("departments") or []
    if depts:
        msg = (f"말씀하신 증상으로는 '{depts[0]['name']}' 진료가 가장 적합해 보여요. "
               "아래 추천 진료과와 주변 의료기관을 확인해보세요.")
    else:
        msg = "추천 진료과를 찾지 못했어요. 증상을 조금 더 알려주시겠어요?"
    return {"final_message": msg}


def build_graph():
    b = StateGraph(AgentState)
    b.add_node("collect", collect.collect_symptoms)
    b.add_node("ask", ask_node)
    b.add_node("triage", triage.check_emergency)
    b.add_node("emergency", emergency_node)
    b.add_node("classify", classify.classify_department)
    b.add_node("search", search.search_hospitals)
    b.add_node("respond", respond_node)

    b.add_edge(START, "collect")
    b.add_conditional_edges("collect", route_after_collect, {"ask": "ask", "triage": "triage"})
    b.add_edge("ask", END)
    b.add_conditional_edges("triage", route_after_triage, {"emergency": "emergency", "classify": "classify"})
    b.add_edge("emergency", END)
    b.add_edge("classify", "search")
    b.add_edge("search", "respond")
    b.add_edge("respond", END)
    return b.compile()


# 앱 시작 시 1회만 컴파일해 재사용
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_agent(req: ChatRequest) -> ChatResponse:
    """ChatRequest → State → 그래프 실행 → ChatResponse."""
    location = {}
    if req.lat is not None:
        location["lat"] = req.lat
    if req.lng is not None:
        location["lng"] = req.lng
    if req.region:
        location["region"] = req.region          # 지역명 (EX-03)

    state: AgentState = {
        "user_input": req.message,
        "history": req.history,
        "location": location,
    }
    final = get_graph().invoke(state)

    return ChatResponse(
        reply=final.get("final_message", ""),
        emergency=final.get("emergency", False),
        need_more_info=final.get("need_more_info", False),
        departments=[DepartmentOut(**d) for d in final.get("departments", [])],
        hospitals=[HospitalOut(**h) for h in final.get("hospitals", [])],
    )
