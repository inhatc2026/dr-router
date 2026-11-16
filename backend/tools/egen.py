"""E-Gen 응급의료기관 API (소유자: 최건)"""
import logging
import sqlite3
import time
from typing import Dict, List

from backend.config import EGEN_API_KEY
from backend.tools import hospital_db
from backend.tools._common import fetch_items, haversine_m

logger = logging.getLogger(__name__)

# 응급의료기관 목록. 조건 없이 부르면 전국(약 530곳)이 좌표와 함께 온다.
# 좌표로 응급실만 골라주는 오퍼레이션은 없어서(getEgytLcinfoInqire는 일반
# 병원까지 섞여 옴), 전국 목록을 받아 직접 거리를 계산한다. (실제 호출로 확인)
ER_LIST = "https://apis.data.go.kr/B552657/ErmctInfoInqireService/getEgytListInfoInqire"

DB_RADII = (5000, 20000, 50000, 200000)  # 응급실은 드물어서 넓게. 가장 가까운 곳은 꼭 안내한다

_CACHE_TTL = 6 * 60 * 60  # 응급실 목록은 거의 안 바뀌므로 6시간 캐시
_cache: Dict = {"at": 0.0, "items": []}


def search_emergency(lat: float, lng: float, top: int = 3) -> List[Dict]:
    """위치 기준 가까운 응급실을 가까운 순으로 반환.
    반환 형식은 hira.search_hospitals 와 같고 kind="응급실", is_open=True(24시간).
    병원 DB가 있으면 DB에서 찾고(API 호출 0번), 없으면 E-Gen API로 찾는다.
    """
    if lat is None or lng is None:
        return []

    if hospital_db.available():
        try:
            found = hospital_db.nearest(
                lambda r: hospital_db.search_kind(hospital_db.ER, lat, lng, r, top), DB_RADII, top)
            return [_out(p) for p, _ in found]
        except sqlite3.Error as e:
            logger.warning("병원 DB 응급실 검색 실패 (%s) — API로 검색", e)

    out = []
    for it in _er_list():
        distance = haversine_m(lat, lng, it.get("wgs84Lat"), it.get("wgs84Lon"))
        if distance is None:
            continue
        out.append(_out({
            "name": it.get("dutyName", ""),
            "distance_m": distance,
            "phone": it.get("dutyTel3") or it.get("dutyTel1") or None,  # Tel3 = 응급실 직통
            "address": it.get("dutyAddr") or None,
            "lat": _float(it.get("wgs84Lat")), "lng": _float(it.get("wgs84Lon")),
        }))
    out.sort(key=lambda h: h["distance_m"])
    return out[:top]


def _out(p: Dict) -> Dict:
    return {**p, "dept": "응급의학과", "kind": hospital_db.ER, "is_open": True}  # 응급실은 24시간 운영


def _er_list() -> List[Dict]:
    if _cache["items"] and time.time() - _cache["at"] < _CACHE_TTL:
        return _cache["items"]
    items = fetch_items(ER_LIST, EGEN_API_KEY, {"pageNo": 1, "numOfRows": 1000}, timeout=10)
    if items:  # 실패(빈 결과)는 캐시하지 않고 다음 호출 때 다시 시도
        _cache.update(at=time.time(), items=items)
    return items or []


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
