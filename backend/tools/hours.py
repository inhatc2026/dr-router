"""병원 진료시간 → 지금 진료중인지(is_open) 판단 (소유자: 최건)

is_open 값
  True  : 지금 진료시간 안
  False : 오늘 휴진이거나 진료시간 밖
  None  : 진료시간 정보 없음

진료시간 출처
  1) 국립중앙의료원 병·의원 찾기 — 요일별 + 공휴일 진료시간.
  2) 심평원 의료기관별상세정보 — 요일별 진료시간 + 점심시간. 공휴일은 휴진 여부 문장만 있다.
  병원 DB(hospital_db.py)를 만들어 두면 두 출처를 미리 합쳐 두므로 요청 때 API를 부르지 않는다.
  DB가 없으면 lookup_schedules()가 API로 찾는다(1→2 순서, 1에서 못 찾은 병원만 2로).
공휴일 판단은 한국천문연구원 특일 정보(임시공휴일 포함)를 연 단위로 받아 캐시한다.

API 경로의 호출 수는 병원 수(N)에 비례하지 않는다:
  (서로 다른 시군구 수) + (1단계에서 못 찾은 병원 수), 그나마 캐시에 있으면 0.

한계
  - 점심시간은 2) 출처에 값이 있을 때만 반영한다.
  - 진료시간 정보가 있는데 그 요일·공휴일 시간이 비어 있으면 휴진으로 본다.
  - 공휴일 API가 실패하면 평일로 간주한다.
"""
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from backend.config import EGEN_API_KEY, HIRA_API_KEY
from backend.tools._common import fetch_items

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋으로 충분

NMC_LIST = "https://apis.data.go.kr/B552657/HsptlAsembySearchService/getHsptlMdcncListInfoInqire"
HIRA_DETAIL = "https://apis.data.go.kr/B551182/MadmDtlInfoService2.8/getDtlInfo2.8"
HOLIDAY = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
# 공공데이터포털 인증키는 계정당 하나라서 기관과 상관없이 같은 키를 쓴다
HOLIDAY_API_KEY = HIRA_API_KEY

NMC_ROWS = 5000            # 가장 큰 시군구(서울 강남구)가 약 3,100곳 (실측)
SCHEDULE_TTL = 12 * 3600   # 진료시간은 거의 안 바뀜
HOLIDAY_TTL = 24 * 3600    # 임시공휴일이 새로 지정될 수 있어 하루마다 갱신
MAX_WORKERS = 8

HOLIDAY_KEY = 8  # 요일 번호: 1=월 … 7=일, 8=공휴일 (병·의원 찾기 API와 같은 규칙)
_HIRA_DAYS = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


@dataclass(frozen=True)
class Schedule:
    days: Dict[int, Tuple[int, int]]  # 요일 번호 → (시작, 종료) HHMM 정수. 예: (930, 1830)
    holiday_known: bool = True        # False면 공휴일 진료 여부를 모름
    lunch: Dict[int, Tuple[int, int]] = field(default_factory=dict)  # 요일 번호 → 점심시간

    def to_json(self) -> str:
        return json.dumps({"days": self.days, "holiday_known": self.holiday_known, "lunch": self.lunch})

    @staticmethod
    def from_json(text: Optional[str]) -> Optional["Schedule"]:
        if not text:
            return None
        d = json.loads(text)
        to_days = lambda m: {int(k): tuple(v) for k, v in (m or {}).items()}  # noqa: E731
        return Schedule(to_days(d.get("days")), bool(d.get("holiday_known", True)), to_days(d.get("lunch")))


_region_cache: Dict[Tuple[str, str], Tuple[float, Tuple[Dict, Dict]]] = {}
_detail_cache: Dict[str, Tuple[float, Optional[Schedule]]] = {}
_holiday_cache: Dict[int, Tuple[float, Set[date]]] = {}


def clear_caches() -> None:
    _region_cache.clear()
    _detail_cache.clear()
    _holiday_cache.clear()


# ── 진료시간 찾기 ──────────────────────────────────────────

def lookup_schedules(records: List[Dict]) -> List[Optional[Schedule]]:
    """병원 목록의 진료시간을 한꺼번에 찾는다.

    records: [{"name", "phone", "addr", "ykiho"}, ...]  (심평원 병원기본목록 값)
    반환: 같은 순서의 [Schedule 또는 None]
    """
    result: List[Optional[Schedule]] = [None] * len(records)
    regions = [_region(r.get("addr")) for r in records]

    # 1단계: 시군구별 목록 — 시군구마다 1번만, 병렬로
    indexes = _parallel(_region_index, {rg for rg in regions if rg})
    pending = []
    for i, (rec, rg) in enumerate(zip(records, regions)):
        index = indexes.get(rg) if rg else None
        item = _match(index, rec.get("name"), rec.get("phone")) if index else None
        result[i] = _schedule_from_nmc(item) if item else None
        if result[i] is None and rec.get("ykiho"):
            pending.append(i)

    # 2단계: 못 찾은 병원만 병원별 상세정보 — 병렬로
    details = _parallel(_hira_detail, {records[i]["ykiho"] for i in pending})
    for i in pending:
        result[i] = details.get(records[i]["ykiho"])
    return result


def _parallel(fn: Callable, keys: Iterable) -> Dict:
    keys = list(keys)
    if not keys:
        return {}
    if len(keys) == 1:
        return {keys[0]: fn(keys[0])}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(keys))) as ex:
        return dict(zip(keys, ex.map(fn, keys)))


def _region(addr) -> Optional[Tuple[str, str]]:
    """주소 → (시도, 시군구). 예: '경기도 수원시 권선구 …' → ('경기도', '수원시')."""
    parts = str(addr or "").split()
    if not parts:
        return None
    # 세종처럼 시군구가 없는 곳은 둘째 칸이 도로명이다
    q1 = parts[1] if len(parts) > 1 and parts[1][-1:] in ("시", "군", "구") else ""
    return parts[0], q1


def _region_index(region: Tuple[str, str]):
    cached = _region_cache.get(region)
    if cached and time.time() - cached[0] < SCHEDULE_TTL:
        return cached[1]
    q0, q1 = region
    items = fetch_items(NMC_LIST, EGEN_API_KEY,
                        {"Q0": q0, "Q1": q1, "pageNo": 1, "numOfRows": NMC_ROWS}, timeout=15)
    if items is None:  # 호출 실패는 캐시하지 않는다
        return None
    if len(items) >= NMC_ROWS:
        logger.warning("%s %s 병·의원이 %s곳 이상 — 일부 누락", q0, q1, NMC_ROWS)
    index = build_index(items)
    _region_cache[region] = (time.time(), index)
    return index


def build_index(items: Iterable[Dict]) -> Tuple[Dict, Dict]:
    """병·의원 찾기 항목들 → (전화번호별, 이름별) 찾아보기 표. _match()에서 쓴다."""
    by_tel: Dict[str, List[Dict]] = {}
    by_name: Dict[str, List[Dict]] = {}
    for it in items:
        tel = _digits(it.get("dutyTel1"))
        if len(tel) >= 7:
            by_tel.setdefault(tel, []).append(it)
        by_name.setdefault(it.get("dutyName"), []).append(it)
    return by_tel, by_name


def _match(index, name, phone) -> Optional[Dict]:
    """전화번호 우선, 그다음 이름으로 같은 병원을 찾는다. 애매하면 None."""
    by_tel, by_name = index
    tel = _digits(phone)
    candidates = by_tel.get(tel, []) if len(tel) >= 7 else []
    for it in candidates:
        if it.get("dutyName") == name:
            return it
    if len(candidates) == 1:
        return candidates[0]
    named = by_name.get(name, [])
    return named[0] if len(named) == 1 else None


def _hira_detail(ykiho: str) -> Optional[Schedule]:
    cached = _detail_cache.get(ykiho)
    if cached and time.time() - cached[0] < SCHEDULE_TTL:
        return cached[1]
    items = fetch_items(HIRA_DETAIL, HIRA_API_KEY, {"ykiho": ykiho}, timeout=10)
    if items is None:  # 호출 실패는 캐시하지 않는다
        return None
    schedule = _schedule_from_hira(items[0]) if items else None
    _detail_cache[ykiho] = (time.time(), schedule)  # '정보 없음'도 캐시해서 재조회 방지
    return schedule


def _schedule_from_nmc(it: Dict) -> Optional[Schedule]:
    days = {}
    for d in range(1, HOLIDAY_KEY + 1):
        span = _span(it.get(f"dutyTime{d}s"), it.get(f"dutyTime{d}c"))
        if span:
            days[d] = span
    return Schedule(days) if days else None


# 공휴일 안내 문장 중 '하루 종일 쉰다'는 뜻으로 확실한 것만 (공백 제거 후 비교).
# '오후 2시 이후 휴진'처럼 일부만 쉬는 문장은 시간을 알 수 없으므로 '모름'으로 둔다.
_HOLIDAY_CLOSED = {"휴진", "전부휴진", "종일휴진", "전일휴진", "휴무", "전부휴무", "종일휴무", "휴일",
                   "공휴일휴진", "진료없음", "없음"}


def _schedule_from_hira(it: Dict) -> Optional[Schedule]:
    days = {}
    for d, name in _HIRA_DAYS.items():
        span = _span(it.get(f"trmt{name}Start"), it.get(f"trmt{name}End"))
        if span:
            days[d] = span
    if not days:
        return None
    holiday_text = "".join(str(it.get("noTrmtHoli") or "").split())
    lunch = {}
    week, sat = _parse_lunch(it.get("lunchWeek")), _parse_lunch(it.get("lunchSat"))
    for d in range(1, 6):
        if week:
            lunch[d] = week
    if sat:
        lunch[6] = sat
    return Schedule(days, holiday_known=holiday_text in _HOLIDAY_CLOSED, lunch=lunch)


def _span(start, end) -> Optional[Tuple[int, int]]:
    s, e = _hhmm(start), _hhmm(end)
    # 시작=종료(예: 일요일 '0'~'0')는 진료하지 않는 날로 적힌 것
    return (s, e) if s is not None and e is not None and s != e else None


_CLOCK = re.compile(r"(\d{1,2})\s*(?:시|:)\s*(?:(\d{1,2})\s*분?)?")


def _parse_lunch(text) -> Optional[Tuple[int, int]]:
    """'13시00분~14시00분', '12:30 ~ 13:30', '1시~2시' 같은 문장 → (1300, 1400). 못 읽으면 None."""
    found = _CLOCK.findall(str(text or ""))
    if len(found) < 2:
        return None
    times = []
    for hour, minute in found[:2]:
        h, m = int(hour), int(minute or 0)
        if h < 7:  # '1시~2시'는 오후
            h += 12
        if h > 23 or m > 59:
            return None
        times.append(h * 100 + m)
    start, end = times
    # 점심시간으로 보기 어려운 값은 버린다 (예: 진료시간을 잘못 적은 경우)
    if not (1000 <= start < end <= 1700 and end - start <= 300):
        return None
    return start, end


def _hhmm(value) -> Optional[int]:
    """'0930', 930, 1830 같은 값 → 930, 1830. 이상한 값은 None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 2400 and n % 100 < 60 else None


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


# ── 지금 진료중인가 ────────────────────────────────────────

def is_open(schedule: Optional[Schedule], now: Optional[datetime] = None) -> Optional[bool]:
    if schedule is None:
        return None
    now = now.astimezone(KST) if now else datetime.now(KST)
    t = now.hour * 100 + now.minute

    today = _day_key(schedule, now.date())
    if today is None:  # 오늘 공휴일인데 공휴일 진료 여부를 모름
        return None
    span = schedule.days.get(today)
    if span and _covers(span, t):
        lunch = schedule.lunch.get(today)
        return not (lunch and lunch[0] <= t < lunch[1])

    # 전날 밤부터 이어지는 진료 (예: 22:00~06:00)
    yesterday = _day_key(schedule, now.date() - timedelta(days=1))
    prev = schedule.days.get(yesterday) if yesterday else None
    if prev and prev[1] < prev[0] and t < prev[1]:
        return True
    return False


def _day_key(schedule: Schedule, d: date) -> Optional[int]:
    if is_holiday(d):
        return HOLIDAY_KEY if schedule.holiday_known else None
    return d.isoweekday()  # 1=월 … 7=일


def _covers(span: Tuple[int, int], t: int) -> bool:
    start, end = span
    if start < end:
        return start <= t < end
    return t >= start  # 자정을 넘기는 진료(종료 < 시작)의 오늘 부분


def is_holiday(d: date) -> bool:
    days = holidays_of(d.year)
    return d in days if days is not None else False


def holidays_of(year: int) -> Optional[Set[date]]:
    """그 해 공휴일. 병원 DB에 저장돼 있으면 DB에서, 없으면 API로 받는다(하루 캐시)."""
    cached = _holiday_cache.get(year)
    if cached and time.time() - cached[0] < HOLIDAY_TTL:
        return cached[1]
    from backend.tools import hospital_db  # hospital_db가 이 모듈을 쓰므로 순환 import를 피해 여기서
    days = None
    if hospital_db.available():
        try:
            days = hospital_db.holidays(year)
        except Exception as e:  # noqa: BLE001  DB 문제면 API로 대신
            logger.warning("병원 DB에서 공휴일 읽기 실패 (%s)", type(e).__name__)
    if days is None:
        days = fetch_holidays(year)
    if days is None:
        logger.warning("%s년 공휴일 조회 실패 — 평일로 간주", year)
        return None
    _holiday_cache[year] = (time.time(), days)
    return days


def fetch_holidays(year: int) -> Optional[Set[date]]:
    """특일 정보 API로 그 해 공휴일(임시·대체공휴일 포함)을 받는다. 실패하면 None."""
    items = fetch_items(HOLIDAY, HOLIDAY_API_KEY, {"solYear": year, "numOfRows": 100}, timeout=10)
    if items is None:
        return None
    days = set()
    for it in items:
        if it.get("isHoliday") != "Y":
            continue
        try:
            days.add(datetime.strptime(str(it["locdate"]), "%Y%m%d").date())
        except (KeyError, ValueError):
            continue
    return days
