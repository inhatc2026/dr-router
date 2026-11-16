"""④ 의료기관 검색 노드 (소유자: 최건)"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from backend.agent.state import AgentState
from backend.tools import egen, hira, hospital_db, pharmacy


def search_hospitals(state: AgentState) -> dict:
    """분류된 진료과 + 사용자 위치로 주변 병원·약국·응급실을 찾는다.
    State 규약대로 바뀐 칸(hospitals)만 돌려준다. 순서: 병원 → 약국 → 응급실 (각각 가까운 순).

    위치: location["region"](예: "인천 미추홀구")이 한 곳으로 정해지면 그 지역 중심, 아니면 lat·lng(GPS).
    (위치 권한 거부 시 지역명 검색: 요구사항 EX-03·FR-29) 위치를 모르면 빈 목록.
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


def find_region(text: str, near: Optional[Tuple[float, float]] = None) -> dict:
    """지역명 판정 (① 증상 수집 단계에서 되묻기·후보 제안에 쓰도록 공개, 요구사항 FR-29·FR-30·EX-11).

    반환: {"status", "point", "name", "candidates"}  — 자세한 뜻은 hospital_db.resolve_region
      "ok"          → 그 지역으로 검색하면 된다 (location["region"]에 넣기)
      "ambiguous"   → candidates가 있으면 후보 제안(near=GPS면 가까운 순), 없으면 더 자세히 되묻기
      "not_found"   → 모르는 지역명 → 다시 묻기
      "unavailable" → 병원 DB가 없어 지역명을 판정할 수 없음
    """
    empty = {"status": "unavailable", "point": None, "name": None, "candidates": []}
    if not hospital_db.available():  # 지역명 검색은 병원 DB가 있어야 한다
        return empty
    try:
        return hospital_db.resolve_region(text, near)
    except sqlite3.Error:
        return empty


def resolve_location(loc: dict) -> Optional[Tuple[float, float]]:
    """location → (위도, 경도).

    사용자가 말한 지역명(region)이 한 곳으로 정해지면 GPS보다 우선한다("중구에서 안과 찾아줘").
    지역명이 모호하거나 모르면 GPS를 쓰고, 둘 다 없으면 None.
    """
    lat, lng = loc.get("lat"), loc.get("lng")
    gps = (lat, lng) if lat is not None and lng is not None else None
    region = loc.get("region")
    if region:
        found = find_region(region, near=gps)
        if found["status"] == "ok":
            return found["point"]
    return gps
