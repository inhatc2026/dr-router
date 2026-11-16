"""전국 병원 DB (SQLite) — 만들기와 검색 (소유자: 최건)

심평원 병원 검색 API는 한 번에 3~16초가 걸린다. 그래서 전국 병원을 미리 받아
SQLite 파일에 넣어 두고, 사용자 요청 때는 이 파일에서만 찾는다(API 호출 0번).

만들기 / 갱신 (RAG의 build_index와 같은 방식, 저장소 루트에서):
    python -m backend.tools.hospital_db
  - 심평원 '전국 병의원 및 약국 현황' 분기 파일(zip)을 받는다. 새 분기 파일이 나왔을 때만 다시 받는다.
  - 국립중앙의료원 병·의원 찾기 API로 전국 진료시간을 받아 병원과 미리 연결해 둔다(약 8번 호출).
  - 새 파일에 다 만든 뒤 바꿔치기하므로, 서버가 켜져 있어도 그대로 실행해도 된다.
  - 진료시간이 바뀔 수 있으니 주 1회 정도 다시 실행하면 충분하다.
  자동 다운로드가 안 되면 https://www.data.go.kr/data/15051059/fileData.do 에서 zip을 직접 받아
    python -m backend.tools.hospital_db --zip 받은파일.zip

DB 파일이 없으면 hira.py가 기존처럼 API로 검색한다(팀원 환경이 깨지지 않도록).
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
from math import cos, radians
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from backend.config import EGEN_API_KEY
from backend.tools import hours
from backend.tools._common import fetch_items, haversine_m

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("HOSPITAL_DB", "data/raw/hospitals.sqlite")  # data/raw/는 .gitignore 대상
RAW_DIR = os.path.dirname(DB_PATH) or "."

# 공공데이터포털 파일데이터 (페이지의 '다운로드' 버튼이 쓰는 주소)
PORTAL_PAGE = "https://www.data.go.kr/data/15051059/fileData.do"
PORTAL_META = "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do"
PORTAL_FILE = "https://www.data.go.kr/cmm/cmm/fileDownload.do"
PORTAL_FORM = {"publicDataPk": "15051059", "publicDataDetailPk": "uddi:e1fd9796-73e3-417a-80ff-c082d9fa080f",
               "atchFileId": "", "fileDetailSn": "1", "publicDataTyCode": "PR0051"}

NMC_PAGE_ROWS = 10000  # 1만 건에 약 3초 (실측), 전국 약 8만 곳

# 분기 파일의 진료시간 칸 → 심평원 상세정보 API 칸 이름 (hours._schedule_from_hira 재사용)
_FILE_DAYS = {"월요일": "Mon", "화요일": "Tue", "수요일": "Wed", "목요일": "Thu",
              "금요일": "Fri", "토요일": "Sat", "일요일": "Sun"}

SCHEMA = """
CREATE TABLE hospital (
    ykiho TEXT PRIMARY KEY,   -- 심평원 암호화요양기호
    name TEXT, kind TEXT, addr TEXT, phone TEXT,
    lat REAL, lng REAL,
    schedule TEXT,            -- hours.Schedule JSON (없으면 NULL = 진료시간 정보 없음)
    schedule_src TEXT         -- nmc / hira / NULL
);
CREATE INDEX hospital_lat_lng ON hospital(lat, lng);
CREATE TABLE subject (code TEXT, ykiho TEXT, PRIMARY KEY (code, ykiho)) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ── 검색 ──────────────────────────────────────────────────

def available() -> bool:
    return os.path.isfile(DB_PATH)


def search(code: str, lat: float, lng: float, radius_m: int, limit: int) -> List[Tuple[Dict, Optional[hours.Schedule]]]:
    """진료과목코드가 있는 병원을 반경 안에서 가까운 순으로. [(병원 dict, 진료시간표), ...]"""
    dlat = radius_m / 111_320
    dlng = radius_m / (111_320 * max(cos(radians(lat)), 0.01))
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT h.name, h.phone, h.lat, h.lng, h.schedule FROM subject s "
            "JOIN hospital h ON h.ykiho = s.ykiho "
            "WHERE s.code = ? AND h.lat BETWEEN ? AND ? AND h.lng BETWEEN ? AND ?",
            (code, lat - dlat, lat + dlat, lng - dlng, lng + dlng)).fetchall()
    finally:
        con.close()
    found = []
    for name, phone, h_lat, h_lng, schedule in rows:
        d = haversine_m(lat, lng, h_lat, h_lng)
        if d is not None and d <= radius_m:
            found.append(({"name": name, "distance_m": d, "phone": phone or None}, schedule))
    found.sort(key=lambda r: r[0]["distance_m"])
    return [(h, hours.Schedule.from_json(s)) for h, s in found[:limit]]


def info() -> Dict[str, str]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()


# ── 만들기 ────────────────────────────────────────────────

def build(zip_path: str, nmc_items: List[Dict], out_path: str = DB_PATH, version: str = "") -> Dict[str, int]:
    """분기 파일(zip) + 병·의원 찾기 목록 → SQLite. 새 파일에 만든 뒤 바꿔치기한다."""
    hosp_rows = _read_csv(zip_path, "1.")
    details = {r["암호화요양기호"]: _file_detail_to_api(r) for r in _read_csv(zip_path, "4.")}
    subjects = [(r["진료과목코드"], r["암호화요양기호"]) for r in _read_csv(zip_path, "5.")]

    nmc_by_region: Dict[Tuple[str, str], List[Dict]] = {}
    for it in nmc_items:
        region = hours._region(it.get("dutyAddr"))
        if region:
            nmc_by_region.setdefault(region, []).append(it)
    indexes = {rg: hours.build_index(items) for rg, items in nmc_by_region.items()}

    stats = {"hospitals": 0, "no_coords": 0, "nmc": 0, "hira": 0, "none": 0, "lunch": 0}
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
        records.append((ykiho, name, r.get("종별코드명"), addr, phone, lat, lng,
                        schedule.to_json() if schedule else None, src))
    stats["hospitals"] = len(records)

    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO hospital VALUES (?,?,?,?,?,?,?,?,?)", records)
        con.executemany("INSERT OR IGNORE INTO subject VALUES (?,?)", subjects)
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("hira_version", version), ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
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


def fetch_nmc_all() -> List[Dict]:
    """병·의원 찾기 API로 전국 목록을 받는다. 한 페이지라도 실패하면 예외(반쪽 DB 방지)."""
    items: List[Dict] = []
    page = 1
    while True:
        batch = None
        for attempt in range(3):  # 공공 API는 가끔 연결이 끊기므로 페이지마다 3번까지 재시도
            batch = fetch_items(hours.NMC_LIST, EGEN_API_KEY,
                                {"pageNo": page, "numOfRows": NMC_PAGE_ROWS}, timeout=120)
            if batch is not None:
                break
            time.sleep(2 * (attempt + 1))
        if batch is None:
            raise RuntimeError(f"병·의원 찾기 API {page}페이지 실패 — 기존 DB를 유지합니다")
        items += batch
        if len(batch) < NMC_PAGE_ROWS:
            return items
        page += 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(description="전국 병원 DB 만들기/갱신")
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
    print(f"[1/3] 심평원 분기 파일: {version}")
    try:
        nmc = fetch_nmc_all()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"[2/3] 병·의원 찾기 진료시간: {len(nmc)}곳")
    stats = build(zip_path, nmc, DB_PATH, version)
    print(f"[3/3] {DB_PATH} 완료 ({time.time() - t:.0f}초)\n"
          f"      병원 {stats['hospitals']}곳 | 진료시간: 병·의원 찾기 {stats['nmc']}, 심평원 {stats['hira']}, "
          f"없음 {stats['none']} | 점심시간 {stats['lunch']} | 좌표 없어 제외 {stats['no_coords']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
