"""주변 약국 검색 (소유자: 최건) — 요구사항 FR-18"""
import logging
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from backend.config import EGEN_API_KEY
from backend.tools import hospital_db, hours
from backend.tools._common import fetch_items, haversine_m

logger = logging.getLogger(__name__)

# DB가 없을 때 쓰는 국립중앙의료원 '좌표 기준 약국' 오퍼레이션 (가까운 약국 목록 + 거리)
PHARMACY_NEAR = "https://apis.data.go.kr/B552657/ErmctInsttInfoInqireService/getParmacyLcinfoInqire"

DB_RADII = (2000, 5000, 20000, 200000)  # 요구사항 FR-21: 2km → 5km → 가장 가까운 곳
_CACHE_TTL = 10 * 60
_cache: Dict[Tuple, Tuple[float, List[Dict]]] = {}


def search_pharmacies(lat: float, lng: float, top: int = 3) -> List[Dict]:
    """위치 기준 가까운 약국을 가까운 순으로 반환.
    반환 형식은 hira.search_hospitals 와 같고 kind="약국", dept="약국".
    is_open은 병원 DB의 운영시간(요일별·공휴일)으로 계산한다. DB 없이 API로 찾으면 None(정보 없음).
    """
    if lat is None or lng is None:
        return []

    if hospital_db.available():
        try:
            found = hospital_db.nearest(
                lambda r: hospital_db.search_kind(hospital_db.PHARMACY, lat, lng, r, top), DB_RADII, top)
            return [_out(p, hours.is_open(schedule)) for p, schedule in found]
        except sqlite3.Error as e:
            logger.warning("병원 DB 약국 검색 실패 (%s) — API로 검색", e)

    key = (round(lat, 3), round(lng, 3))
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        rows = cached[1]
    else:
        items = fetch_items(PHARMACY_NEAR, EGEN_API_KEY,
                            {"WGS84_LON": lng, "WGS84_LAT": lat, "pageNo": 1, "numOfRows": 50}, timeout=10)
        rows = []
        for it in items or []:
            d = haversine_m(lat, lng, it.get("latitude"), it.get("longitude"))
            if d is not None:
                rows.append({"name": it.get("dutyName", ""), "distance_m": d,
                             "phone": it.get("dutyTel1") or None, "address": it.get("dutyAddr") or None,
                             "lat": _float(it.get("latitude")), "lng": _float(it.get("longitude"))})
        rows.sort(key=lambda p: p["distance_m"])
        if rows:
            _cache[key] = (time.time(), rows)
    return [_out(p, None) for p in rows[:top]]


def _out(p: Dict, is_open: Optional[bool]) -> Dict:
    return {**p, "dept": "약국", "kind": hospital_db.PHARMACY, "is_open": is_open}


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
