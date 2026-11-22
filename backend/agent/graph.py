"""
LangGraph 그래프 정의 (소유자: 박근령)

흐름:
  START → triage ─(응급?)─ 예 → emergency(119 + 응급실) → END
               │ 아니오
               ▼
             collect ─(정보 충분?)─ 아니오 → ask(되묻기) → END
                        │ 예
                        ▼
                     classify → search → respond → END

- 응급 분기: search.search_emergency_rooms 로 가까운 응급실을 hospitals에 채운다 (최건, FR-08·IF-03)
- region : run_agent에서 ChatRequest.region → location["region"] 로 넘기고,
           검색 쪽(search.resolve_location)이 지역명을 GPS보다 우선 사용한다 (EX-03)
           지역명이 모호/미확인이면 collect에서 되묻는다 (FR-29·FR-30)
"""
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import BoundedSemaphore

from langgraph.graph import StateGraph, START, END

from backend.api.schemas import ChatRequest, ChatResponse, DepartmentOut, HospitalOut
from backend.agent.state import AgentState
from backend.agent.nodes import collect, triage, classify, search

logger = logging.getLogger(__name__)
ER_LOOKUP_BUDGET_SECONDS = 0.5
_er_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="emergency-lookup")
_er_slots = BoundedSemaphore(2)


def lookup_emergency_rooms(state: AgentState) -> dict:
    """응답 대기만 0.5초로 제한한다. 느린 조회는 최대 2개이고 대기열은 만들지 않는다."""
    if not _er_slots.acquire(blocking=False):
        return {"hospitals": []}
    try:
        future = _er_executor.submit(search.search_emergency_rooms, state.copy())
    except Exception:
        _er_slots.release()
        raise
    future.add_done_callback(lambda _: _er_slots.release())
    try:
        return future.result(timeout=ER_LOOKUP_BUDGET_SECONDS)
    except TimeoutError:
        # 실행 중 HTTP 호출은 강제로 중단할 수 없다. 늦은 결과는 응답에 반영하지 않는다.
        future.cancel()
        return {"hospitals": []}


# ── 분기(라우터) 함수: 반환 문자열이 다음 노드 이름 ──
def route_after_collect(state: AgentState) -> str:
    return "ask" if state.get("need_more_info") else "classify"


def route_after_triage(state: AgentState) -> str:
    return "emergency" if state.get("emergency") else "collect"


# ── 메시지를 작성하는 터미널 노드들 ──
def ask_node(state: AgentState) -> dict:
    """정보 부족/지역 되묻기 — collect가 넣어둔 질문이 있으면 그걸 쓴다."""
    msg = state.get("final_message") or "증상을 조금 더 자세히 알려주시겠어요? (언제부터 · 어디가 · 어떻게)"
    return {"final_message": msg, "need_more_info": True}


def emergency_node(state: AgentState) -> dict:
    """응급: 119 안내 + 가까운 응급실을 hospitals에 채운다."""
    try:
        found = lookup_emergency_rooms(state)
    except Exception as error:
        # 부가 정보 조회 실패 때문에 응급 안내까지 실패시키지 않는다.
        # 예외 메시지에는 위치 등 원문이 섞일 수 있으므로 타입만 기록한다.
        logger.warning("응급실 조회 실패 (%s)", type(error).__name__)
        found = {"hospitals": []}
    return {
        "final_message": "응급 증상이 의심됩니다. 즉시 119에 연락하거나 가까운 응급실을 방문하세요.",
        **found,
        "need_more_info": False,
        "departments": [],
    }


def respond_node(state: AgentState) -> dict:
    depts = state.get("departments") or []
    if depts and all(d.get("sources") for d in depts) and depts[0].get("score", 0) >= 0.5:
        msg = "증상과 참고 문서를 바탕으로 진료과 후보를 안내합니다. 의학적 진단이 아니며, 정확한 판단은 의료기관을 방문해 상담해 주세요."
    elif depts:
        msg = "참고 진료과 후보와 주변 의료기관을 확인해 주세요. 근거 확인이 필요하므로 의료기관을 방문해 상담해 주세요."
    else:
        msg = "진료과를 판단할 정보가 부족합니다. 의료기관을 방문해 상담해 주세요."
    region = (state.get("location") or {}).get("region")
    if region:
        msg += f" 검색 요청 지역: {region}."
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

    b.add_edge(START, "triage")
    b.add_conditional_edges("collect", route_after_collect, {"ask": "ask", "classify": "classify"})
    b.add_edge("ask", END)
    b.add_conditional_edges("triage", route_after_triage, {"emergency": "emergency", "collect": "collect"})
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
