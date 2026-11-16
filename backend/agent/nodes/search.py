"""④ 의료기관 검색 노드 (소유자: 최건)"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from backend.agent.state import AgentState
from backend.tools import egen, hira, hospital_db, pharmacy


def search_hospitals(state: AgentState) -> dict:
    """분류된 진료과 + 사용자 위치로 주변 병원·약국·응급실을 찾는다.
    State 규약대로 바뀐 칸(hospitals)만 돌려준다. 순서: 병원 → 약국 → 응급실 (각각 가까운 순).

    위치: location의 lat·lng를 쓰고, 없으면 location["region"](예: "인천 미추홀구")으로
    그 지역 중심을 찾는다(위치 권한 거부 시, 요구사항 EX-03). 위치를 모르면 빈 목록.
    병원은 추천 진료과(1순위)가 있을 때만 찾고, 약국·응급실은 위치만 있으면 찾는다.
    """
    point = resolve_location(state.get("location") or {})
    if point is None:
        return {"hospitals": []}
    lat, lng = point
    depts = state.get("departments") or []
    top_dept = depts[0]["name"] if depts else None

    # 세 검색은 서로 독립이라 동시에 부른다
    with ThreadPoolExecutor(max_workers=3) as ex:
        near = ex.submit(hira.search_hospitals, top_dept, lat, lng) if top_dept else None
        drug = ex.submit(pharmacy.search_pharmacies, lat, lng)
        er = ex.submit(egen.search_emergency, lat, lng)
        hospitals = (near.result() if near else []) + drug.result() + er.result()
    return {"hospitals": hospitals}


def search_emergency_rooms(state: AgentState) -> dict:
    """응급 분기용 노드: 가까운 응급실만 찾아 hospitals 칸으로 돌려준다.
    (요구사항 FR-08·FR-10·IF-03 — 응급이면 진료과 분류·일반 병원 검색은 건너뛰고 응급실은 안내)
    위치를 모르면 빈 목록 → 화면은 119 안내만 보여 주면 된다."""
    point = resolve_location(state.get("location") or {})
    return {"hospitals": egen.search_emergency(*point) if point else []}


def resolve_location(loc: dict) -> Optional[Tuple[float, float]]:
    """location → (위도, 경도). 좌표가 없으면 지역명으로 찾고, 그래도 모르면 None."""
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat is not None and lng is not None:
        return lat, lng
    region = loc.get("region")
    if region and hospital_db.available():  # 지역명 검색은 병원 DB가 있어야 한다
        try:
            return hospital_db.locate(region)
        except sqlite3.Error:
            return None
    return None
