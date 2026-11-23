"""E-Gen 응급의료기관 API (소유자: 최건)"""
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from backend.config import EGEN_API_KEY
from backend.tools import hospital_db
from backend.tools._common import fetch_items, haversine_m

logger = logging.getLogger(__name__)

# 응급의료기관 목록. 조건 없이 부르면 전국(약 530곳)이 좌표와 함께 온다.
# 좌표로 응급실만 골라주는 오퍼레이션은 없어서(getEgytLcinfoInqire는 일반
# 병원까지 섞여 옴), 전국 목록을 받아 직접 거리를 계산한다. (실제 호출로 확인)
ER_LIST = "https://apis.data.go.kr/B552657/ErmctInfoInqireService/getEgytListInfoInqire"
# 응급실 실시간 가용병상. 조건 없이 부르면 전국(약 410곳, 0.4초 안팎). hvec = 응급실 일반 가용병상(음수면 과밀)
ER_BEDS = "https://apis.data.go.kr/B552657/ErmctInfoInqireService/getEmrrmRltmUsefulSckbdInfoInqire"

DB_RADII = (5000, 20000, 50000, 200000)  # 응급실은 드물어서 넓게. 가장 가까운 곳은 꼭 안내한다

_CACHE_TTL = 6 * 60 * 60  # 응급실 목록은 거의 안 바뀌므로 6시간 캐시
_cache: Dict = {"at": 0.0, "items": []}

# ── 실시간 병상 ───────────────────────────────────────────
# 응답은 병상 API를 기다리지 않는다(응급 응답은 응급실 조회 전체가 0.5초 안이어야 함).
# 캐시를 읽기만 하고, 오래됐으면 뒤에서 한 번만 갱신한다. 캐시가 아예 비었을 때만 잠깐 기다린다.
BEDS_TTL = 60                 # 이보다 오래되면 뒤에서 갱신 (사용자가 있을 때만 호출 → 호출 수 절약)
BEDS_MAX_AGE = 10 * 60        # 캐시가 이보다 오래되면(갱신이 계속 실패) 병상을 표시하지 않는다
BEDS_ROW_MAX_AGE = 60 * 60    # 기관이 입력한 시각이 이보다 오래되면 그 기관은 '정보 없음'
BEDS_COLD_WAIT = 0.3          # 캐시가 비었을 때 기다리는 최대 시간 (0.5초 예산 안)
POOL = 6                      # 병상을 보고 고를 후보 수
PROMOTE_RANGE_M = 10_000      # 가장 가까운 곳보다 이만큼 넘게 먼 곳은 병상이 있어도 앞당기지 않는다

_beds: Dict = {"enabled": False, "at": 0.0, "by_id": {}, "thread": None}
_beds_lock = threading.Lock()


def enable_realtime_beds() -> None:
    """서버 시작 시 켜고 뒤에서 한 번 미리 받아 둔다(routes.py). 첫 응급 요청부터 병상이 붙도록.
    서버를 띄우지 않는 테스트에서는 꺼져 있어 병상 API를 부르지 않는다.
    FastAPI가 라우터의 시작 함수를 두 번 부를 수 있어(앱 복사 + 라우터 lifespan) 한 번만 동작하게 한다."""
    with _beds_lock:
        if _beds["enabled"]:
            return
        _beds["enabled"] = True
        _start_refresh_if_stale()


def disable_realtime_beds() -> None:
    """서버 종료 시 끈다. 같은 프로세스에서 뒤이어 도는 테스트가 병상 API를 부르지 않게."""
    _beds["enabled"] = False


def _start_refresh_if_stale() -> Optional[threading.Thread]:
    """_beds_lock 안에서 부른다. 캐시가 오래됐고 갱신 중이 아니면 뒤에서 갱신을 시작한다."""
    worker = _beds["thread"]
    if time.time() - _beds["at"] > BEDS_TTL and not (worker and worker.is_alive()):
        worker = threading.Thread(target=_refresh_beds, name="er-beds-refresh", daemon=True)
        _beds["thread"] = worker
        worker.start()
    return worker


def search_emergency(lat: float, lng: float, top: int = 3) -> List[Dict]:
    """위치 기준 가까운 응급실 top곳.
    반환 형식은 hira.search_hospitals 와 같고 kind="응급실", is_open=True(24시간).
    병원 DB가 있으면 DB에서 찾고(목록 API 호출 0번), 없으면 E-Gen API로 찾는다.

    실시간 병상(er_beds)이 0 이하(포화)인 곳은 가까운 후보 안에서 뒤로 보낸다.
    병상 정보가 없는 곳(실시간 정보를 안 주는 기관)은 그대로 둔다 — 가장 가까운 곳이 이유 없이 빠지면 위험.
    포화인 곳도 빼지는 않는다(입력 지연·오류 가능). 다른 후보가 모자라면 그대로 안내한다.
    """
    if lat is None or lng is None:
        return []
    beds = _beds_snapshot()

    if hospital_db.available():
        try:
            found = hospital_db.nearest(
                lambda r: hospital_db.search_kind(hospital_db.ER, lat, lng, r, POOL, with_id=True), DB_RADII, POOL)
            return _pick([p for p, _ in found], beds, top)
        except sqlite3.Error as e:
            logger.warning("병원 DB 응급실 검색 실패 (%s) — API로 검색", e)

    out = []
    for it in _er_list():
        distance = haversine_m(lat, lng, it.get("wgs84Lat"), it.get("wgs84Lon"))
        if distance is None:
            continue
        out.append({
            "id": it.get("hpid"),
            "name": it.get("dutyName", ""),
            "distance_m": distance,
            "phone": it.get("dutyTel3") or it.get("dutyTel1") or None,  # Tel3 = 응급실 직통
            "address": it.get("dutyAddr") or None,
            "lat": _float(it.get("wgs84Lat")), "lng": _float(it.get("wgs84Lon")),
        })
    out.sort(key=lambda h: h["distance_m"])
    return _pick(out[:POOL], beds, top)


def _pick(cands: List[Dict], beds: Dict, top: int) -> List[Dict]:
    """가까운 순 후보 → 병상을 붙이고, 가장 가까운 곳 기준 PROMOTE_RANGE_M 안에서 포화인 곳만 뒤로 보낸다."""
    out = [_out(p, beds.get(p.get("id"))) for p in cands]
    if not out:
        return []
    limit = out[0]["distance_m"] + PROMOTE_RANGE_M
    near = [h for h in out if h["distance_m"] <= limit]
    far = [h for h in out if h["distance_m"] > limit]
    near.sort(key=lambda h: (_is_full(h["er_beds"]), h["distance_m"]))
    return (near + far)[:top]


def _is_full(beds: Optional[int]) -> bool:
    return beds is not None and beds <= 0  # 0 이하 = 포화(음수는 과밀). None(정보 없음)은 포화로 보지 않는다


def _out(p: Dict, bed: Optional[tuple] = None) -> Dict:
    p = {k: v for k, v in p.items() if k != "id"}
    count, at = bed if bed else (None, None)
    return {**p, "dept": "응급의학과", "kind": hospital_db.ER, "is_open": True,  # 응급실은 24시간 운영
            "er_beds": count, "er_beds_at": at.isoformat(timespec="minutes") if at else None}


def _beds_snapshot() -> Dict:
    """hpid → (가용병상, 기관 입력 시각). 꺼져 있거나 모르면 {}. 네트워크를 기다리지 않는다(캐시가 빌 때만 잠깐)."""
    if not _beds["enabled"]:
        return {}
    with _beds_lock:
        worker = _start_refresh_if_stale()
        current = _fresh_beds()
    if not current and worker and worker.is_alive():
        worker.join(BEDS_COLD_WAIT)
        with _beds_lock:
            current = _fresh_beds()
    return current


def _fresh_beds() -> Dict:
    return _beds["by_id"] if time.time() - _beds["at"] <= BEDS_MAX_AGE else {}


def _refresh_beds() -> None:
    """전국 응급실 실시간 병상을 받아 캐시에 넣는다. 실패하면 이전 캐시를 그대로 둔다."""
    try:
        items = fetch_items(ER_BEDS, EGEN_API_KEY, {"pageNo": 1, "numOfRows": 1000}, timeout=5)
        if not items:
            return
        now = datetime.now()
        by_id = {}
        for it in items:
            count, at = _int(it.get("hvec")), _parse_time(it.get("hvidate"))
            if it.get("hpid") and count is not None and at and (now - at).total_seconds() <= BEDS_ROW_MAX_AGE:
                by_id[it["hpid"]] = (count, at)
        with _beds_lock:
            _beds.update(at=time.time(), by_id=by_id)
    except Exception as e:  # noqa: BLE001 — 병상은 부가 정보라 어떤 실패도 응급실 안내를 막으면 안 된다
        logger.warning("응급실 실시간 병상 갱신 실패 (%s)", type(e).__name__)


def _er_list() -> List[Dict]:
    if _cache["items"] and time.time() - _cache["at"] < _CACHE_TTL:
        return _cache["items"]
    items = fetch_items(ER_LIST, EGEN_API_KEY, {"pageNo": 1, "numOfRows": 1000}, timeout=10)
    if items:  # 실패(빈 결과)는 캐시하지 않고 다음 호출 때 다시 시도
        _cache.update(at=time.time(), items=items)
    return items or []


def _parse_time(value) -> Optional[datetime]:
    """hvidate 20261123195023 → datetime (기관 입력 시각, 한국 시간)."""
    try:
        return datetime.strptime(str(value)[:14], "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
