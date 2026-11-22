"""전국 의료기관 DB (SQLite) — 만들기와 검색 (소유자: 최건)

심평원 병원 검색 API는 한 번에 3~16초가 걸린다. 그래서 전국 병원·약국·응급실과 공휴일을
미리 받아 SQLite 파일에 넣어 두고, 사용자 요청 때는 이 파일에서만 찾는다(API 호출 0번).
공공 API가 막혀도 검색이 끝까지 동작한다(요구사항 IF-08, NFR-06).

만들기 / 갱신 (RAG의 build_index와 같은 방식, 저장소 루트에서):
    python -m backend.tools.hospital_db
  - 심평원 '전국 병의원 및 약국 현황' 분기 파일(zip)을 받는다. 새 분기 파일이 나왔을 때만 다시 받는다.
  - 국립중앙의료원 API로 병·의원 진료시간(약 8번), 약국(약 3번), 응급실(1번)을 받는다.
  - 공휴일(올해·내년)을 받아 둔다.
  - 새 파일에 다 만든 뒤 바꿔치기하므로, 서버가 켜져 있어도 그대로 실행해도 된다.
  - 진료시간이 바뀔 수 있으니 주 1회 정도 다시 실행하면 충분하다.
  자동 다운로드가 안 되면 https://www.data.go.kr/data/15051059/fileData.do 에서 zip을 직접 받아
    python -m backend.tools.hospital_db --zip 받은파일.zip

DB 파일이 없거나 형식이 옛날 것이면 각 검색이 기존처럼 API로 찾는다(팀원 환경이 깨지지 않도록).
"""
import argparse
import csv
import io
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import date, datetime
from math import cos, radians
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import requests

from backend.config import EGEN_API_KEY
from backend.tools import hours
from backend.tools._common import fetch_items, haversine_m

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("HOSPITAL_DB", "data/raw/hospitals.sqlite")  # data/raw/는 .gitignore 대상
RAW_DIR = os.path.dirname(DB_PATH) or "."
SCHEMA_VERSION = "4"  # 표 구조를 바꾸면 올린다 → 옛 DB는 쓰지 않고 다시 만들라고 안내 (3: 지역명 색인, 4: 과목별 전문의 수)

# 공공데이터포털 파일데이터 (페이지의 '다운로드' 버튼이 쓰는 주소)
PORTAL_PAGE = "https://www.data.go.kr/data/15051059/fileData.do"
PORTAL_META = "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do"
PORTAL_FILE = "https://www.data.go.kr/cmm/cmm/fileDownload.do"
PORTAL_FORM = {"publicDataPk": "15051059", "publicDataDetailPk": "uddi:e1fd9796-73e3-417a-80ff-c082d9fa080f",
               "atchFileId": "", "fileDetailSn": "1", "publicDataTyCode": "PR0051"}

# 국립중앙의료원 약국 목록 (운영시간·좌표 포함, 전국 약 2.5만 곳)
PHARMACY_LIST = "https://apis.data.go.kr/B552657/ErmctInsttInfoInqireService/getParmacyListInfoInqire"
PAGE_ROWS = 10000  # 1만 건에 약 2~3초 (실측)

HOSPITAL, PHARMACY, ER = "병원", "약국", "응급실"  # place.kind 값 = 응답의 kind 값

# 분기 파일의 진료시간 칸 → 심평원 상세정보 API 칸 이름 (hours._schedule_from_hira 재사용)
_FILE_DAYS = {"월요일": "Mon", "화요일": "Tue", "수요일": "Wed", "목요일": "Thu",
              "금요일": "Fri", "토요일": "Sat", "일요일": "Sun"}

SCHEMA = """
CREATE TABLE place (
    kind TEXT,                -- 병원 / 약국 / 응급실
    id TEXT,                  -- 병원: 심평원 암호화요양기호, 약국·응급실: 국립중앙의료원 기관ID
    name TEXT, addr TEXT, phone TEXT,
    lat REAL, lng REAL,
    schedule TEXT,            -- hours.Schedule JSON (없으면 NULL = 진료시간 정보 없음)
    schedule_src TEXT,        -- nmc / hira / NULL
    PRIMARY KEY (kind, id)
);
CREATE INDEX place_kind_lat_lng ON place(kind, lat, lng);
CREATE TABLE subject (           -- 병원 진료과목
    code TEXT, id TEXT,
    specialists INTEGER DEFAULT 0,  -- 그 과목 전문의 수 (검색 때 전문의 있는 병원을 먼저)
    PRIMARY KEY (code, id)
) WITHOUT ROWID;
CREATE TABLE holiday (day TEXT PRIMARY KEY);  -- 'YYYY-MM-DD'
CREATE TABLE area (           -- 지역명 색인: 병원 주소의 행정구역 하나당 한 줄 (resolve_region용)
    unit TEXT,                -- '서울특별시', '중구', '태평로1가' …
    id TEXT,                  -- place.id (병원)
    units TEXT,               -- 그 병원의 행정구역 전체, 탭으로 구분
    lat REAL, lng REAL
);
CREATE INDEX area_unit ON area(unit);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ── 검색 ──────────────────────────────────────────────────

_checked = {"key": None, "ok": False}


def available() -> bool:
    """DB 파일이 있고 지금 코드와 같은 형식인지. (파일이 바뀔 때만 다시 확인)"""
    try:
        st = os.stat(DB_PATH)
    except OSError:
        return False
    key = (DB_PATH, st.st_mtime_ns, st.st_size)
    if _checked["key"] != key:
        try:
            ok = info().get("schema_version") == SCHEMA_VERSION
        except sqlite3.Error:
            ok = False
        if not ok:
            logger.warning("병원 DB가 깨졌거나 옛 형식입니다 — python -m backend.tools.hospital_db 를 다시 실행하세요. "
                           "그때까지는 API로 검색합니다.")
        _checked.update(key=key, ok=ok)
    return _checked["ok"]


def _db_key():
    """DB 파일이 바뀌면(다시 만들면) 달라지는 값 — 캐시를 파일별로 나누는 데 쓴다."""
    try:
        st = os.stat(DB_PATH)
    except OSError:
        return None
    return DB_PATH, st.st_mtime_ns, st.st_size


def search(codes: Union[str, Sequence[str]], lat: float, lng: float, radius_m: int,
           limit: int, prefer_specialist: bool = False) -> List[Tuple[Dict, Optional[hours.Schedule]]]:
    """진료과목코드 중 하나라도 있는 병원을 반경 안에서 가까운 순으로. [(장소 dict, 진료시간표), ...]

    prefer_specialist=True면 그 과 전문의가 있는 병원을 먼저, 없는 병원을 뒤에(각각 가까운 순) 둔다.
    (진료과목만 등록한 다른 과 의원 — 예: 외과를 찾는데 산부인과의원 — 이 1위로 나오는 것을 막는다)
    """
    codes = (codes,) if isinstance(codes, str) else tuple(codes)
    marks = ",".join("?" * len(codes))
    # 그 과목들 중 가장 많은 전문의 수. 치과처럼 코드가 여러 개여도 병원당 한 번만 나온다.
    # NULL이면 그 과목을 등록하지 않은 병원 → 제외
    spec = f"(SELECT MAX(s.specialists) FROM subject s WHERE s.id = p.id AND s.code IN ({marks}))"
    return _near(HOSPITAL, lat, lng, radius_m, limit, spec, codes, prefer_specialist)


def search_kind(kind: str, lat: float, lng: float, radius_m: int,
                limit: int) -> List[Tuple[Dict, Optional[hours.Schedule]]]:
    """약국·응급실을 반경 안에서 가까운 순으로."""
    return _near(kind, lat, lng, radius_m, limit)


def nearest(fetch, radii: Sequence[int], limit: int):
    """반경을 차례로 넓혀 가며 limit개가 모일 때까지 찾는다. fetch(radius) → 결과 목록."""
    found = []
    for radius in radii:
        found = fetch(radius)
        if len(found) >= limit:
            break
    return found


def _near(kind, lat, lng, radius_m, limit, spec_sql=None, spec_args=(), prefer_spec=False):
    dlat = radius_m / 111_320
    dlng = radius_m / (111_320 * max(cos(radians(lat)), 0.01))
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT p.name, p.phone, p.addr, p.lat, p.lng, p.schedule, {spec_sql or 0} AS spec FROM place p "
            "WHERE p.kind = ? AND p.lat BETWEEN ? AND ? AND p.lng BETWEEN ? AND ?"
            + (" AND spec IS NOT NULL" if spec_sql else ""),
            (*(spec_args if spec_sql else ()), kind, lat - dlat, lat + dlat, lng - dlng, lng + dlng)).fetchall()
    finally:
        con.close()
    found = []
    for name, phone, addr, p_lat, p_lng, schedule, spec in rows:
        d = haversine_m(lat, lng, p_lat, p_lng)
        if d is not None and d <= radius_m:
            found.append(({"name": name, "distance_m": d, "phone": phone or None,
                           "address": addr or None, "lat": p_lat, "lng": p_lng}, schedule, spec or 0))
    if prefer_spec:
        found.sort(key=lambda r: (r[2] <= 0, r[0]["distance_m"]))
    else:
        found.sort(key=lambda r: r[0]["distance_m"])
    return [(p, hours.Schedule.from_json(s)) for p, s, _ in found[:limit]]


# ── 지역명 → 좌표 (요구사항 FR-29·FR-30·EX-03·EX-11) ─────────

REGION_OK, REGION_AMBIGUOUS, REGION_NOT_FOUND = "ok", "ambiguous", "not_found"
_ADMIN_SUFFIX = ("특별시", "광역시", "특별자치시", "특별자치도", "도", "시", "군", "구", "읍", "면", "동", "가", "리")
_COMPACT_M = 15000  # 한 지역으로 볼 범위: 80%가 중심에서 15km 안
_DOMINANT = 0.95    # 한 곳이 95% 이상이면 그곳으로 본다 ('신사동'은 강남구 93%·은평구 7% → 되묻기)


def resolve_region(text: str, near: Optional[Tuple[float, float]] = None) -> Dict:
    """지역명을 판정한다. 외부 지도 API 없이 DB의 병원 주소(행정구역 부분)로 찾는다.

    반환: {"status", "point", "name", "candidates"}
      ok         : 한 곳으로 정해짐 → point=(위도, 경도), name="서울특별시 강남구"
      ambiguous  : '중구'(5개 도시)·'제주'(제주시·서귀포시)처럼 여러 곳 → candidates에 후보
                   [{"name","lat","lng","count","distance_m"}] (near=GPS를 주면 가까운 순, 아니면 병원 많은 순)
                   후보가 비어 있으면 범위가 너무 넓은 것 → 더 자세히 되묻기
      not_found  : 모르는 지역명
    시군구·읍면동 단위까지 지원한다. 도로명('세종대로', '강남대로')에는 걸리지 않는다.
    DB를 만들 때 미리 만든 행정구역 색인(area 표)으로 찾고 결과를 캐시하므로 보통 수 ms다.
    (그래프의 응급실 조회 대기 0.5초 안에 들어와야 한다)
    """
    tokens = tuple(t for t in re.split(r"[\s,]+", str(text or "").strip()) if t)[:4]
    found = _analyze_region(tokens) if tokens else (REGION_NOT_FOUND,)
    result = {"status": found[0], "point": None, "name": None, "candidates": []}
    if found[0] == REGION_OK:
        return {**result, "point": found[1], "name": found[2]}
    if found[0] == REGION_AMBIGUOUS:  # GPS 거리는 부를 때마다 계산 (캐시는 GPS와 무관하게)
        candidates = [{"name": name, "lat": lat, "lng": lng, "count": count,
                       "distance_m": haversine_m(near[0], near[1], lat, lng) if near else None}
                      for name, (lat, lng), count in found[1]]
        candidates.sort(key=(lambda c: c["distance_m"]) if near else (lambda c: -c["count"]))
        return {**result, "candidates": candidates[:5]}
    return result


_region_cache: Dict[Tuple, Tuple] = {}
_REGION_CACHE_MAX = 512


def _analyze_region(tokens: Tuple[str, ...]) -> Tuple:
    """→ ("ok", 중심, 이름) / ("ambiguous", [(이름, 중심, 병원 수)...]) / ("not_found",). DB 파일별 캐시."""
    key = (_db_key(), tokens)
    if key in _region_cache:
        return _region_cache[key]
    found = _analyze_region_uncached(tokens)
    if len(_region_cache) >= _REGION_CACHE_MAX:
        _region_cache.clear()
    _region_cache[key] = found
    return found


def _analyze_region_uncached(tokens: Tuple[str, ...]) -> Tuple:
    longest = max(tokens, key=len)
    con = _connect()
    try:  # 가장 긴 단어로 시작하는 행정구역을 색인으로 찾은 뒤, 나머지 단어도 모두 맞는지 확인한다
        rows = con.execute("SELECT DISTINCT id, units, lat, lng FROM area WHERE unit >= ? AND unit < ?",
                           (longest, longest + "\U0010FFFF")).fetchall()
        if not rows and con.execute("SELECT 1 FROM area LIMIT 1").fetchone() is None:
            # 색인이 빈 DB(place에 직접 넣어 만든 테스트 DB 등) → 주소를 직접 훑는다 (느리지만 결과는 같음)
            escape = longest.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = [(pid, "\t".join(_admin_units(addr)), lat, lng) for pid, addr, lat, lng in con.execute(
                "SELECT id, addr, lat, lng FROM place WHERE kind = ? AND addr LIKE ? ESCAPE '\\'",
                (HOSPITAL, f"%{escape}%"))]
    finally:
        con.close()
    matched = []
    for _id, units, lat, lng in rows:
        units = units.split("\t")
        if all(any(u.startswith(t) for u in units) for t in tokens):
            matched.append((units, lat, lng))
    if not matched:
        return (REGION_NOT_FOUND,)

    # 95% 기준: 병원 DB 주소 기준 병원 수 비율 (요구사항 v0.6 FR-29)
    groups = _significant(_group(matched, 2), len(matched))
    top = max(groups, key=lambda k: len(groups[k]))
    if len(groups[top]) >= _DOMINANT * len(matched):  # '강남'(강남구 99.5%), '세종'(세종시 99%)은 그곳으로
        groups = {top: groups[top]}
    if len(groups) == 1:
        (name, pts), = groups.items()
        center = _center(pts)
        if _compact(center, pts):
            return (REGION_OK, center, name)
        groups = _significant(_group(pts, 3), len(pts))  # 넓은 시(예: 용인시)는 구 단위 후보로
        if len(groups) == 1:
            groups = {}  # 더 나눌 수 없을 만큼 넓음 → 후보 없이 되묻기
    return (REGION_AMBIGUOUS, [(name, _center(pts), len(pts)) for name, pts in groups.items()])


def locate(region: str) -> Optional[Tuple[float, float]]:
    """지역명이 한 곳으로 정해지면 그 중심 좌표, 아니면 None. (자세한 판정은 resolve_region)"""
    return resolve_region(region)["point"]


def _admin_units(addr: str) -> List[str]:
    """주소의 행정구역 부분만. '서울특별시 중구 세종대로 124, 3층 (태평로1가)' → ['서울특별시','중구','태평로1가']"""
    addr = str(addr or "")
    units = []
    for part in addr.split()[:4]:
        part = part.strip(",")
        if not part.endswith(_ADMIN_SUFFIX):
            break  # 도로명부터는 행정구역이 아님
        units.append(part)
    tail = re.search(r"\(([^()]*)\)\s*$", addr)  # 끝의 괄호 속 법정동
    if tail:
        units += [x.strip() for x in tail.group(1).split(",") if x.strip().endswith(("동", "가", "리", "읍", "면"))]
    return units


def _group(points, level: int) -> Dict[str, List[Tuple[List[str], float, float]]]:
    """[(행정구역, 위도, 경도)] → {"서울특별시 중구": [(행정구역, 위도, 경도), ...]}. level 2=시도·시군구, 3=+구"""
    groups: Dict[str, List] = {}
    for units, lat, lng in points:
        key = units[:1]
        if len(units) > 1 and units[1].endswith(("시", "군", "구")):
            key = units[:2]
            if level >= 3 and len(units) > 2 and units[2].endswith("구"):
                key = units[:3]
        groups.setdefault(" ".join(key), []).append((units, lat, lng))
    return groups


def _significant(groups, total: int):
    """주소 표기 잡음(한두 곳)을 뺀 후보만. 3곳 이상이거나 전체의 5% 이상"""
    return {k: v for k, v in groups.items() if len(v) >= 3 or len(v) >= 0.05 * total}


def _center(pts) -> Tuple[float, float]:
    pts = [p[-2:] for p in pts]
    return median(p[0] for p in pts), median(p[1] for p in pts)


def _compact(center, pts) -> bool:
    pts = [p[-2:] for p in pts]
    inside = sum(1 for lat, lng in pts if (haversine_m(center[0], center[1], lat, lng) or 0) <= _COMPACT_M)
    return inside >= 0.8 * len(pts)


def holidays(year: int) -> Optional[Set[date]]:
    """DB에 저장된 그 해 공휴일. 그 해가 저장돼 있지 않으면 None."""
    con = _connect()
    try:
        stored = {v for (v,) in con.execute("SELECT value FROM meta WHERE key = 'holiday_years'")}
        if str(year) not in ",".join(stored).split(","):
            return None
        days = con.execute("SELECT day FROM holiday WHERE day LIKE ?", (f"{year}-%",)).fetchall()
    finally:
        con.close()
    return {date.fromisoformat(d) for (d,) in days}


def info() -> Dict[str, str]:
    con = _connect()
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


# ── 만들기 ────────────────────────────────────────────────

def build(zip_path: str, nmc_items: List[Dict], out_path: str = DB_PATH, version: str = "",
          pharmacies: Iterable[Dict] = (), ers: Iterable[Dict] = (),
          holiday_days: Optional[Dict[int, Set[date]]] = None) -> Dict[str, int]:
    """분기 파일(zip) + 병·의원 찾기 + 약국 + 응급실 + 공휴일 → SQLite. 새 파일에 만든 뒤 바꿔치기한다.

    holiday_days: {연도: 공휴일 집합}. 받은 연도만 저장하고, 없는 연도는 검색 때 API로 받는다.
    """
    hosp_rows = _read_csv(zip_path, "1.")
    details = {r["암호화요양기호"]: _file_detail_to_api(r) for r in _read_csv(zip_path, "4.")}
    subjects = [(r["진료과목코드"], r["암호화요양기호"], _int(r.get("과목별 전문의수")))
                for r in _read_csv(zip_path, "5.")]

    nmc_by_region: Dict[Tuple[str, str], List[Dict]] = {}
    for it in nmc_items:
        region = hours._region(it.get("dutyAddr"))
        if region:
            nmc_by_region.setdefault(region, []).append(it)
    indexes = {rg: hours.build_index(items) for rg, items in nmc_by_region.items()}

    stats = {"hospitals": 0, "no_coords": 0, "nmc": 0, "hira": 0, "none": 0, "lunch": 0,
             "pharmacies": 0, "ers": 0, "holidays": 0}
    records = []
    for r in hosp_rows:
        try:
            lng, lat = float(r["좌표(X)"]), float(r["좌표(Y)"])
        except (KeyError, ValueError):
            stats["no_coords"] += 1
            continue
        ykiho, name, phone, addr = r["암호화요양기호"], r["요양기관명"], r.get("전화번호"), r.get("주소")
        index = indexes.get(hours._region(addr))
        nmc = hours._match(index, name, phone) if index else None
        from_nmc = hours._schedule_from_nmc(nmc) if nmc else None
        from_hira = hours._schedule_from_hira(details[ykiho]) if ykiho in details else None
        if from_nmc:  # 공휴일 진료시간까지 있는 쪽을 우선, 점심시간은 심평원 쪽에서 가져온다
            schedule = hours.Schedule(from_nmc.days, True, from_hira.lunch if from_hira else {})
            src = "nmc"
        else:
            schedule, src = from_hira, ("hira" if from_hira else None)
        stats[src or "none"] += 1
        stats["lunch"] += bool(schedule and schedule.lunch)
        records.append((HOSPITAL, ykiho, name, addr, phone, lat, lng,
                        schedule.to_json() if schedule else None, src))
    stats["hospitals"] = len(records)

    for kind, items, key in ((PHARMACY, pharmacies, "pharmacies"), (ER, ers, "ers")):
        for it in items:
            try:
                lat, lng = float(it["wgs84Lat"]), float(it["wgs84Lon"])
            except (KeyError, TypeError, ValueError):
                stats["no_coords"] += 1
                continue
            schedule = hours._schedule_from_nmc(it) if kind == PHARMACY else None  # 응급실은 24시간
            phone = (it.get("dutyTel3") or it.get("dutyTel1")) if kind == ER else it.get("dutyTel1")
            records.append((kind, it.get("hpid") or it.get("dutyName"), it.get("dutyName", ""), it.get("dutyAddr"),
                            phone, lat, lng, schedule.to_json() if schedule else None,
                            "nmc" if schedule else None))
            stats[key] += 1

    holiday_days = holiday_days or {}
    days = sorted({d.isoformat() for ds in holiday_days.values() for d in ds})
    stats["holidays"] = len(days)

    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        con.executemany("INSERT OR IGNORE INTO place VALUES (?,?,?,?,?,?,?,?,?)", records)
        con.executemany("INSERT OR IGNORE INTO subject VALUES (?,?,?)", subjects)
        con.executemany("INSERT INTO area VALUES (?,?,?,?,?)", _area_rows(records))
        con.executemany("INSERT INTO holiday VALUES (?)", [(d,) for d in days])
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("schema_version", SCHEMA_VERSION), ("hira_version", version),
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("holiday_years", ",".join(str(y) for y in sorted(holiday_days))),
            *[(f"stat_{k}", str(v)) for k, v in stats.items()]])
        con.commit()
    finally:
        con.close()
    os.replace(tmp, out_path)
    return stats


def _int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _area_rows(records):
    """병원 주소의 행정구역마다 한 줄씩 → 지역명 색인. (place 레코드: kind, id, name, addr, …, lat, lng, …)"""
    for kind, pid, _name, addr, _phone, lat, lng, *_ in records:
        if kind != HOSPITAL:
            continue
        units = _admin_units(addr)
        joined = "\t".join(units)
        for unit in dict.fromkeys(units):  # 중복 제거, 순서 유지
            yield unit, pid, joined, lat, lng


def _read_csv(zip_path: str, prefix: str) -> List[Dict]:
    with zipfile.ZipFile(zip_path) as z:
        info_ = next((i for i in z.infolist() if _zip_name(i).startswith(prefix)), None)
        if info_ is None:
            raise ValueError(f"zip 안에 '{prefix}'로 시작하는 파일이 없음 — 파일 구성이 바뀌었는지 확인")
        raw = z.read(info_)
    for enc in ("utf-8-sig", "cp949"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{prefix} 파일 인코딩을 읽을 수 없음")


def _zip_name(i: zipfile.ZipInfo) -> str:
    if i.flag_bits & 0x800:  # UTF-8 파일명
        return i.filename
    try:
        return i.filename.encode("cp437").decode("cp949")  # 한국어 Windows에서 만든 zip
    except (UnicodeEncodeError, UnicodeDecodeError):
        return i.filename


def _file_detail_to_api(r: Dict) -> Dict:
    d = {"lunchWeek": r.get("점심시간_평일"), "lunchSat": r.get("점심시간_토요일"), "noTrmtHoli": r.get("휴진안내_공휴일")}
    for kr, en in _FILE_DAYS.items():
        d[f"trmt{en}Start"], d[f"trmt{en}End"] = r.get(f"진료시작시간_{kr}"), r.get(f"진료종료시간_{kr}")
    return d


# ── 받아오기 ──────────────────────────────────────────────

def download_hira_zip(force: bool = False) -> Tuple[str, str]:
    """최신 분기 파일을 data/raw/에 받는다. 같은 버전이 이미 있으면 다시 받지 않는다. → (경로, 버전)"""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    s.get(PORTAL_PAGE, timeout=30)
    meta = s.post(PORTAL_META, data=PORTAL_FORM, timeout=30,
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": PORTAL_PAGE}).json()
    if not meta.get("status"):
        raise RuntimeError(f"포털 응답 이상: {meta.get('error')}")
    version = meta["dataSetFileDetailInfo"].get("dataNm") or meta["atchFileId"]
    path = os.path.join(RAW_DIR, re.sub(r"[^\w.-]", "_", version) + ".zip")
    if os.path.isfile(path) and not force:
        return path, version
    os.makedirs(RAW_DIR, exist_ok=True)
    with s.get(PORTAL_FILE, params={"atchFileId": meta["atchFileId"], "fileDetailSn": meta["fileDetailSn"]},
               headers={"Referer": PORTAL_PAGE}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if not zipfile.is_zipfile(path + ".part"):
        os.remove(path + ".part")
        raise RuntimeError("받은 파일이 zip이 아님 — 포털 다운로드 방식이 바뀌었을 수 있음")
    os.replace(path + ".part", path)
    return path, version


def fetch_all_pages(url: str, label: str) -> List[Dict]:
    """목록 API를 끝 페이지까지 받는다. 한 페이지라도 끝내 실패하면 예외(반쪽 DB 방지)."""
    items: List[Dict] = []
    page = 1
    while True:
        batch = None
        for attempt in range(3):  # 공공 API는 가끔 연결이 끊기므로 페이지마다 3번까지 재시도
            batch = fetch_items(url, EGEN_API_KEY, {"pageNo": page, "numOfRows": PAGE_ROWS}, timeout=120)
            if batch is not None:
                break
            time.sleep(2 * (attempt + 1))
        if batch is None:
            raise RuntimeError(f"{label} API {page}페이지 실패 — 기존 DB를 유지합니다")
        items += batch
        if len(batch) < PAGE_ROWS:
            return items
        page += 1


def fetch_nmc_all() -> List[Dict]:
    return fetch_all_pages(hours.NMC_LIST, "병·의원 찾기")


def fetch_pharmacies_all() -> List[Dict]:
    return fetch_all_pages(PHARMACY_LIST, "약국")


def fetch_ers_all() -> List[Dict]:
    from backend.tools import egen  # egen이 이 모듈을 쓰므로 순환 import를 피해 여기서
    return fetch_all_pages(egen.ER_LIST, "응급실")


def fetch_holidays(years: Iterable[int]) -> Dict[int, Set[date]]:
    """받을 수 있는 연도만 담는다(내년 공휴일은 아직 발표 전일 수 있음)."""
    out = {}
    for y in years:
        days = hours.fetch_holidays(y)
        if days:
            out[y] = days
    return out


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(description="전국 의료기관 DB 만들기/갱신")
    p.add_argument("--zip", help="직접 받은 심평원 분기 파일 zip 경로 (자동 다운로드 대신)")
    p.add_argument("--force", action="store_true", help="같은 버전이어도 zip을 다시 받기")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    t = time.time()
    if args.zip:
        zip_path, version = args.zip, os.path.basename(args.zip)
    else:
        try:
            zip_path, version = download_hira_zip(args.force)
        except Exception as e:  # noqa: BLE001
            print(f"분기 파일 자동 다운로드 실패: {e}\n"
                  f"{PORTAL_PAGE} 에서 직접 받아 --zip 경로로 실행하세요.", file=sys.stderr)
            return 1
    print(f"[1/5] 심평원 분기 파일: {version}")
    try:
        nmc = fetch_nmc_all()
        print(f"[2/5] 병·의원 진료시간: {len(nmc)}곳")
        pharmacies = fetch_pharmacies_all()
        print(f"[3/5] 약국: {len(pharmacies)}곳")
        ers = fetch_ers_all()
        print(f"[4/5] 응급실: {len(ers)}곳")
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    this_year = datetime.now(hours.KST).year
    holiday_days = fetch_holidays([this_year, this_year + 1])
    stats = build(zip_path, nmc, DB_PATH, version, pharmacies, ers, holiday_days)
    print(f"[5/5] {DB_PATH} 완료 ({time.time() - t:.0f}초)\n"
          f"      병원 {stats['hospitals']}곳 | 진료시간: 병·의원 찾기 {stats['nmc']}, 심평원 {stats['hira']}, "
          f"없음 {stats['none']} | 점심시간 {stats['lunch']}\n"
          f"      약국 {stats['pharmacies']}곳 | 응급실 {stats['ers']}곳 | "
          f"공휴일 {stats['holidays']}일 ({', '.join(map(str, sorted(holiday_days))) or '받지 못함'}) | "
          f"좌표 없어 제외 {stats['no_coords']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
