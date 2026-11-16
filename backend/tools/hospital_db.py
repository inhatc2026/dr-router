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
SCHEMA_VERSION = "2"  # 표 구조를 바꾸면 올린다 → 옛 DB는 쓰지 않고 다시 만들라고 안내

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
CREATE TABLE subject (code TEXT, id TEXT, PRIMARY KEY (code, id)) WITHOUT ROWID;  -- 병원 진료과목
CREATE TABLE holiday (day TEXT PRIMARY KEY);  -- 'YYYY-MM-DD'
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


def search(codes: Union[str, Sequence[str]], lat: float, lng: float, radius_m: int,
           limit: int) -> List[Tuple[Dict, Optional[hours.Schedule]]]:
    """진료과목코드 중 하나라도 있는 병원을 반경 안에서 가까운 순으로. [(장소 dict, 진료시간표), ...]"""
    codes = (codes,) if isinstance(codes, str) else tuple(codes)
    marks = ",".join("?" * len(codes))
    # 치과처럼 코드가 여러 개면 한 병원이 여러 코드에 걸리므로 EXISTS로 한 번만 센다
    extra = f"AND EXISTS (SELECT 1 FROM subject s WHERE s.id = p.id AND s.code IN ({marks}))"
    return _near(HOSPITAL, lat, lng, radius_m, limit, extra, codes)


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


def _near(kind, lat, lng, radius_m, limit, extra="", extra_args=()):
    dlat = radius_m / 111_320
    dlng = radius_m / (111_320 * max(cos(radians(lat)), 0.01))
    con = _connect()
    try:
        rows = con.execute(
            "SELECT p.name, p.phone, p.addr, p.lat, p.lng, p.schedule FROM place p "
            f"WHERE p.kind = ? AND p.lat BETWEEN ? AND ? AND p.lng BETWEEN ? AND ? {extra}",
            (kind, lat - dlat, lat + dlat, lng - dlng, lng + dlng, *extra_args)).fetchall()
    finally:
        con.close()
    found = []
    for name, phone, addr, p_lat, p_lng, schedule in rows:
        d = haversine_m(lat, lng, p_lat, p_lng)
        if d is not None and d <= radius_m:
            found.append(({"name": name, "distance_m": d, "phone": phone or None,
                           "address": addr or None, "lat": p_lat, "lng": p_lng}, schedule))
    found.sort(key=lambda r: r[0]["distance_m"])
    return [(p, hours.Schedule.from_json(s)) for p, s in found[:limit]]


def locate(region: str) -> Optional[Tuple[float, float]]:
    """'인천 미추홀구', '서울 강남구 역삼동' 같은 지역명 → 그 지역의 중심 좌표(위도, 경도).

    위치 권한을 거부한 사용자용(요구사항 EX-03). 외부 지도 API 없이, DB에 있는 병원 주소 중
    지역명이 모두 들어간 곳들의 가운데(중앙값) 좌표를 쓴다.
    못 찾거나 '중구'처럼 여러 도시에 걸쳐 애매하면 None (지역을 더 자세히 물어보면 된다).
    """
    tokens = [t for t in re.split(r"[\s,]+", str(region or "").strip()) if t][:4]
    if not tokens:
        return None
    escape = lambda t: t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # noqa: E731
    where = " AND ".join("addr LIKE ? ESCAPE '\\'" for _ in tokens)
    con = _connect()
    try:
        rows = con.execute(f"SELECT lat, lng FROM place WHERE kind = ? AND {where}",
                           (HOSPITAL, *[f"%{escape(t)}%" for t in tokens])).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    lat, lng = median(r[0] for r in rows), median(r[1] for r in rows)
    near = sum(1 for r in rows if (haversine_m(lat, lng, r[0], r[1]) or 0) <= 15000)
    if near < 0.8 * len(rows):  # 같은 이름의 지역이 여러 곳 (예: 서울 중구 / 부산 중구)
        return None
    return lat, lng


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
    subjects = [(r["진료과목코드"], r["암호화요양기호"]) for r in _read_csv(zip_path, "5.")]

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
        con.executemany("INSERT OR IGNORE INTO subject VALUES (?,?)", subjects)
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
