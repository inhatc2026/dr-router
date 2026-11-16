"""④ 의료기관 검색 노드 (소유자: 최건)"""
from concurrent.futures import ThreadPoolExecutor

from backend.agent.state import AgentState
from backend.tools import egen, hira


def search_hospitals(state: AgentState) -> dict:
    """분류된 진료과 + 사용자 위치로 공공 API를 호출해 병원/응급실을 찾는다.
    State 규약대로 바뀐 칸(hospitals)만 돌려준다."""
    depts = state.get("departments", [])
    loc = state.get("location", {}) or {}
    top_dept = depts[0]["name"] if depts else None

    hospitals = []
    if top_dept and loc.get("lat") and loc.get("lng"):
        # 병원 검색과 응급실 검색은 서로 독립이라 동시에 부른다
        with ThreadPoolExecutor(max_workers=2) as ex:
            near = ex.submit(hira.search_hospitals, top_dept, loc["lat"], loc["lng"])
            er = ex.submit(egen.search_emergency, loc["lat"], loc["lng"])
            hospitals = near.result() + er.result()

    return {"hospitals": hospitals}
