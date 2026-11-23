"""시연 전 점검 — 실제 병원 DB로 요구사항 시나리오와 응답 시간을 확인한다 (소유자: 최건)

    python -X utf8 -m eval.check_live            # 실제 병원 DB로 시나리오·응답 시간·API 장애 대비 점검
    python -X utf8 -m eval.check_live --live     # + 실제 공공 API 7종 상태, 응급실 실시간 병상, DB 진료시간 최신성 표본 비교
    python -X utf8 -m eval.check_live --md 결과.md  # 결과를 Markdown 표로도 저장 (발표·문서용)

- 병원 DB(python -m backend.tools.hospital_db)가 있어야 한다.
- 분류(classify)가 아직 스텁이면, 병원이 나와야 하는 시나리오는 "분류 결과 가정"으로 진료과를 넣어 돌린다.
- 응답 시간은 서버 안에서 잰 값이다(브라우저·네트워크 시간 제외). 각 시나리오를 3번 돌려 첫 회와 중앙값을 보인다.
- 실패가 하나라도 있으면 종료 코드 1 → 시연 직전에 돌려 보고 통과를 확인하면 된다.
- 인증키와 위치 원문은 출력하지 않는다.
"""
import argparse
import logging
import statistics
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional
from unittest import mock

import requests
from fastapi.testclient import TestClient

from backend.agent import graph
from backend.agent.nodes import classify
from backend.main import app
from backend.tools import _common, egen, hira, hospital_db, hours, pharmacy

NORMAL_LIMIT_S, EMERGENCY_LIMIT_S = 5.0, 1.0  # 요구사항 NFR-07
RUNS = 3

INHA = {"lat": 37.4480, "lng": 126.6575}      # 인하공전 근처
SEOUL = {"lat": 37.5665, "lng": 126.9780}     # 서울시청
BUSAN = {"lat": 35.1796, "lng": 129.0756}     # 부산시청
ULLEUNG = {"lat": 37.4845, "lng": 130.9057}   # 울릉도 (반경 밖 지역)

results: List[Dict] = []


def record(group, name, ok, detail, seconds=None, cold=None, limit=None, warn=False):
    results.append({"group": group, "name": name, "ok": ok, "warn": warn, "detail": detail,
                    "seconds": seconds, "cold": cold, "limit": limit})
    mark = "통과" if ok and not warn else ("주의" if ok else "실패")
    timing = f" | {seconds * 1000:.0f}ms (첫 회 {cold * 1000:.0f}ms, 기준 {limit:.0f}초)" if seconds is not None else ""
    print(f"  [{mark}] {name}{timing}\n         {detail}")


def depts(*names):
    return [{"name": n, "score": round(0.9 - i * 0.2, 1), "reason": "점검용 가정"} for i, n in enumerate(names)]


class Classified:
    """분류가 스텁이므로 정해 둔 진료과를 돌려주게 바꿔 끼운다(그래프 다시 만들기 포함)."""

    def __init__(self, departments):
        self.patch = mock.patch.object(classify, "classify_department",
                                       new=lambda state: {"departments": departments})

    def __enter__(self):
        self.patch.start()
        graph._graph = None

    def __exit__(self, *exc):
        self.patch.stop()
        graph._graph = None


def chat(client, body, runs=RUNS):
    times, data = [], None
    for _ in range(runs):
        t = time.perf_counter()
        r = client.post("/api/chat", json=body)
        times.append(time.perf_counter() - t)
        r.raise_for_status()
        data = r.json()
    return data, times[0], statistics.median(times)


def kinds(data):
    out: Dict[str, int] = {}
    for h in data.get("hospitals", []):
        out[h.get("kind", "?")] = out.get(h.get("kind", "?"), 0) + 1
    return out


def scenario(client, name, body, check: Callable[[dict], Optional[str]], departments=None,
             limit=NORMAL_LIMIT_S, runs=RUNS):
    """check(data)가 None이면 통과, 문자열이면 실패 이유."""
    try:
        if departments:
            with Classified(departments):
                data, cold, med = chat(client, body, runs)
        else:
            data, cold, med = chat(client, body, runs)
    except Exception as e:  # noqa: BLE001
        record("시나리오", name, False, f"오류: {type(e).__name__}")
        return None
    problem = check(data)
    hs = [h for h in data.get("hospitals", []) if h.get("kind") == "병원"][:2]
    first = ", ".join(f"{h['name']}({h['dept']}, {h['distance_m']}m)" for h in hs)
    detail = f"종류별 {kinds(data) or '없음'}" + (f" · 병원 {first}" if first else "") + \
             (f" · 분류 가정 {[d['name'] for d in departments]}" if departments else "")
    if problem:
        detail = f"{problem} — {detail}"
    record("시나리오", name, problem is None, detail, med, cold, limit, warn=problem is None and cold > limit)
    return data


def need(*conds):
    for ok, why in conds:
        if not ok:
            return why
    return None


def in_region(d, region, max_m=3000):
    """지역명 검색 결과 확인: 1위 병원은 그 지역 주소, 나머지는 지역 중심에서 max_m 안.
    (지역 중심에서 반경으로 찾으므로 바로 옆 구의 가까운 병원이 섞이는 것은 정상)"""
    hs = [h for h in d["hospitals"] if h["kind"] == "병원"]
    return need((hs, "병원 없음"), (hs and region in (hs[0].get("address") or ""), f"1위가 {region}가 아님"),
                (all(h["distance_m"] <= max_m for h in hs), f"지역 중심에서 {max_m}m 밖 병원이 섞임"))


def run_scenarios():
    print("\n■ /api/chat 시나리오 (요구사항 6장 + NFR-07)")
    client = TestClient(app)

    def full_list(d, dept=None):
        k = kinds(d)
        hs = d.get("hospitals", [])
        return need((k.get("병원", 0) >= 1, "병원 없음"), (k.get("약국", 0) >= 1, "약국 없음"),
                    (k.get("응급실", 0) >= 1, "응급실 없음"),
                    (all(h.get("address") and h.get("lat") is not None for h in hs), "주소·좌표 빠진 항목 있음"),
                    (dept is None or all(h["dept"] == dept for h in hs if h["kind"] == "병원"), f"병원 진료과가 {dept}가 아님"))

    scenario(client, "일반 증상 + 위치 허용 (인하공전)", {"message": "무릎이 붓고 계단 내려갈 때 시큰거려요", **INHA},
             lambda d: full_list(d, "정형외과"), depts("정형외과", "재활의학과"))
    scenario(client, "요구사항 시나리오: 오른쪽 아랫배 + 발열 (서울)", {"message": "어제부터 오른쪽 아랫배가 아프고 열이 나요", **SEOUL},
             lambda d: full_list(d, "외과"), depts("외과", "소화기내과"))
    scenario(client, "응급 + 위치 허용", {"message": "갑자기 가슴이 쥐어짜듯 아프고 숨이 안 쉬어져요", **SEOUL},
             lambda d: need((d["emergency"], "응급 판정 안 됨"), (kinds(d) == {"응급실": 3}, "응급실 3곳이 아님")),
             limit=EMERGENCY_LIMIT_S)
    scenario(client, "응급 + 지역명만 (위치 거부)", {"message": "아버지가 쓰러졌어요", "region": "인천 미추홀구"},
             lambda d: need((d["emergency"], "응급 판정 안 됨"), (kinds(d).get("응급실", 0) >= 1, "응급실이 0건")),
             limit=EMERGENCY_LIMIT_S)
    scenario(client, "응급 + 위치 없음", {"message": "피를 토했어요"},
             lambda d: need((d["emergency"], "응급 판정 안 됨"), ("119" in d["reply"], "119 안내 없음")),
             limit=EMERGENCY_LIMIT_S)
    scenario(client, "위치 거부 + 지역명 (서울 강남구 역삼동)", {"message": "귀가 먹먹하고 어지러워요", "region": "서울 강남구 역삼동"},
             lambda d: need((not d["need_more_info"], "되물음")) or in_region(d, "서울특별시 강남구"), depts("이비인후과"))
    first = scenario(client, "모호한 지역 + GPS (부산에서 '중구')", {"message": "눈이 충혈되고 이물감이 있어요", "region": "중구", **BUSAN},
                     lambda d: need((d["need_more_info"], "되묻지 않음"), ("부산광역시 중구" in d["reply"], "가까운 후보를 제안하지 않음")),
                     depts("안과"))
    if first:
        hist = [{"role": "user", "content": "눈이 충혈되고 이물감이 있어요"}, {"role": "assistant", "content": first["reply"]}]
        scenario(client, "  └ 후보 확인 '네'", {"message": "네", "region": "중구", "history": hist, **BUSAN},
                 lambda d: need((not d["need_more_info"], "또 되물음")) or in_region(d, "부산광역시 중구"), depts("안과"))
    scenario(client, "모호한 지역 + GPS 없음 ('제주')", {"message": "피부에 붉은 발진이 생기고 가려워요", "region": "제주"},
             lambda d: need((d["need_more_info"], "되묻지 않음")), depts("피부과"))
    scenario(client, "대표 지역명 ('강남' → 되묻지 않음)", {"message": "피부에 붉은 발진이 생기고 가려워요", "region": "강남"},
             lambda d: need((not d["need_more_info"], "되물음"), (kinds(d).get("병원", 0) >= 1, "병원 없음")), depts("피부과"))
    scenario(client, "치과 증상", {"message": "어금니가 욱신거리고 찬물을 마시면 이가 시려요", **SEOUL},
             lambda d: full_list(d, "치과"), depts("치과"))
    scenario(client, "별칭 ('소아과' → 소아청소년과)", {"message": "아이가 열이 나요", **INHA},
             lambda d: full_list(d, "소아청소년과"), depts("소아과"))
    scenario(client, "반경 밖 지역 (울릉도에서 안과)", {"message": "눈이 침침해요", **ULLEUNG},
             lambda d: need((kinds(d).get("병원", 0) >= 1, "가장 가까운 기관도 안내하지 않음")), depts("안과"))

    # 외부 API 장애 (IF-08·NFR-06): 네트워크를 모두 막아도 끝까지 동작해야 한다
    calls = []

    def blocked(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("점검용 차단")

    hours.clear_caches()
    with mock.patch("requests.get", blocked), mock.patch("requests.post", blocked):
        scenario(client, "외부 API 전면 차단 (IF-08)", {"message": "무릎이 아파요", **INHA},
                 lambda d: full_list(d), depts("정형외과"), runs=1)
    record("시나리오", "  └ 차단 상태에서 시도한 네트워크 호출", not calls, f"{len(calls)}번")

    # 실시간 병상은 서버가 켜질 때만 동작한다. 병상 API가 막혀도 응급 안내는 1초 안에 나와야 한다
    with BedsOn(), mock.patch("requests.get", blocked), mock.patch("requests.post", blocked):
        scenario(client, "응급 + 실시간 병상 API 차단", {"message": "갑자기 가슴이 쥐어짜듯 아프고 숨이 안 쉬어져요", **INHA},
                 lambda d: need((kinds(d) == {"응급실": 3}, "응급실 3곳이 아님"),
                                (all("er_beds" not in h for h in d["hospitals"]), "차단됐는데 병상이 붙음")),
                 limit=EMERGENCY_LIMIT_S, runs=1)


class BedsOn:
    """서버 시작 때처럼 실시간 병상 조회를 켠다(캐시는 비운 상태에서 시작). 끝나면 끈다."""

    def __enter__(self):
        egen._beds.update(enabled=False, at=0.0, by_id={}, thread=None)
        egen.enable_realtime_beds()
        return self

    def wait(self):
        worker = egen._beds["thread"]
        if worker:
            worker.join(10)

    def __exit__(self, *exc):
        self.wait()
        egen.disable_realtime_beds()
        egen._beds.update(at=0.0, by_id={}, thread=None)


def check_live_beds():
    """실제 병상 API로 응급 응답에 병상이 붙는지, 포화인 곳이 뒤로 가는지 본다."""
    client = TestClient(app)
    with BedsOn() as beds:
        beds.wait()
        known = len(egen._beds["by_id"])
        record("API", "  └ 실시간 병상 캐시", known > 300, f"병상 정보가 있는 응급실 {known}곳 (최근 1시간 안 입력)")
        data = scenario(client, "응급 + 실시간 병상 (인하공전)", {"message": "갑자기 가슴이 쥐어짜듯 아프고 숨이 안 쉬어져요", **INHA},
                        lambda d: need((kinds(d) == {"응급실": 3}, "응급실 3곳이 아님"),
                                       (any("er_beds" in h for h in d["hospitals"]), "병상이 붙은 응급실이 없음")),
                        limit=EMERGENCY_LIMIT_S)
        if data:
            print("         " + " · ".join(f"{h['name']} {h['distance_m']}m 병상 {h.get('er_beds', '정보 없음')}"
                                          for h in data["hospitals"]))


def check_db():
    print("\n■ 병원 DB")
    if not hospital_db.available():
        record("DB", "병원 DB 사용 가능", False, f"{hospital_db.DB_PATH} 없음 또는 옛 형식 → python -m backend.tools.hospital_db")
        return False
    meta = hospital_db.info()
    stat = lambda k: int(meta.get(f"stat_{k}", 0))  # noqa: E731
    built = datetime.strptime(meta["built_at"], "%Y-%m-%d %H:%M:%S")
    age = (datetime.now() - built).days
    record("DB", "만든 날짜", True, f"{meta['built_at']} ({age}일 전) · 원본 {meta.get('hira_version')}",
           warn=age > 7)
    if age > 7:
        print("         → 진료시간이 바뀌었을 수 있습니다. 시연 전에 python -m backend.tools.hospital_db 로 갱신하세요.")
    with_hours = stat("nmc") + stat("hira")
    record("DB", "기관 수", stat("hospitals") > 70000 and stat("pharmacies") > 20000 and stat("ers") > 400,
           f"병원 {stat('hospitals'):,} · 약국 {stat('pharmacies'):,} · 응급실 {stat('ers'):,}")
    record("DB", "병원 진료시간 보유율", with_hours / max(stat("hospitals"), 1) >= 0.95,
           f"{with_hours / max(stat('hospitals'), 1):.1%} ({with_hours:,}곳) · 점심시간 {stat('lunch'):,}곳")
    year = datetime.now(hours.KST).year
    stored = meta.get("holiday_years", "")
    record("DB", "올해 공휴일 저장", str(year) in stored.split(","), f"저장된 연도: {stored or '없음'}")
    return True


def check_live_apis():
    print("\n■ 실제 공공 API (--live)")
    lat, lng = SEOUL["lat"], SEOUL["lng"]
    probes = [
        ("심평원 병원정보", hira.BASE, hira.HIRA_API_KEY,
         {"numOfRows": 5, "xPos": lng, "yPos": lat, "radius": 1000, "dgsbjtCd": "05"}, 15),
        ("심평원 상세정보", hours.HIRA_DETAIL, hours.HIRA_API_KEY, {"ykiho": "-"}, 10),
        ("병·의원 찾기 (진료시간)", hours.NMC_LIST, hours.EGEN_API_KEY, {"Q0": "서울특별시", "Q1": "중구", "numOfRows": 5}, 10),
        ("약국", hospital_db.PHARMACY_LIST, hours.EGEN_API_KEY, {"Q0": "서울특별시", "Q1": "중구", "numOfRows": 5}, 10),
        ("E-Gen 응급실", egen.ER_LIST, hours.EGEN_API_KEY, {"numOfRows": 5}, 10),
        ("E-Gen 실시간 병상", egen.ER_BEDS, hours.EGEN_API_KEY, {"numOfRows": 5}, 10),
        ("특일 정보 (공휴일)", hours.HOLIDAY, hours.HOLIDAY_API_KEY, {"solYear": datetime.now().year, "numOfRows": 5}, 10),
    ]
    for label, url, key, params, timeout in probes:
        t = time.perf_counter()
        items = _common.fetch_items(url, key, params, timeout=timeout)
        dt = time.perf_counter() - t
        record("API", label, items is not None, f"응답 {len(items) if items is not None else '실패'}건",
               dt, dt, timeout, warn=items is not None and dt > 5)
    print("    (심평원 병원정보는 평소에도 3~16초 걸립니다. 병원 DB가 있으면 검색에는 쓰이지 않습니다.)")
    if hospital_db.available():
        check_live_beds()


def check_freshness():
    """DB의 진료시간이 지금 API 값과 같은지 표본 비교 (서울 중구·종로구 의원)."""
    print("\n■ DB 진료시간 최신성 (--live)")
    con = hospital_db._connect()
    try:
        rows = con.execute("SELECT name, phone, addr, schedule FROM place WHERE kind='병원' AND schedule_src='nmc' "
                           "AND (addr LIKE '서울특별시 중구 %' OR addr LIKE '서울특별시 종로구 %') LIMIT 400").fetchall()
    finally:
        con.close()
    live = {}
    for q1 in ("중구", "종로구"):
        items = _common.fetch_items(hours.NMC_LIST, hours.EGEN_API_KEY, {"Q0": "서울특별시", "Q1": q1, "numOfRows": 5000}, 30)
        if items is None:
            record("최신성", "표본 비교", False, f"병·의원 찾기 API 실패 ({q1})")
            return
        live[q1] = hours.build_index(items)
    same = compared = 0
    for name, phone, addr, schedule in rows[:200]:
        q1 = "중구" if "서울특별시 중구" in addr else "종로구"
        it = hours._match(live[q1], name, phone)
        if not it:
            continue
        compared += 1
        now = hours._schedule_from_nmc(it)
        same += bool(now and hours.Schedule.from_json(schedule).days == now.days)
    rate = same / compared if compared else 0
    record("최신성", "DB 진료시간 = 현재 API", compared >= 50 and rate >= 0.9,
           f"{compared}곳 비교, {same}곳 일치 ({rate:.1%})", warn=compared >= 50 and rate < 0.97)


def write_md(path):
    lines = [f"# 시연 전 점검 결과 ({datetime.now():%Y-%m-%d %H:%M})", "",
             "| 구분 | 항목 | 결과 | 응답 시간(중앙값 / 첫 회) | 내용 |", "|---|---|---|---|---|"]
    for r in results:
        mark = "통과" if r["ok"] and not r["warn"] else ("주의" if r["ok"] else "실패")
        t = f"{r['seconds'] * 1000:.0f}ms / {r['cold'] * 1000:.0f}ms" if r["seconds"] is not None else ""
        lines.append(f"| {r['group']} | {r['name'].strip(' └')} | {mark} | {t} | {r['detail']} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nMarkdown 저장: {path}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="시연 전 점검")
    p.add_argument("--live", action="store_true", help="실제 공공 API 상태·DB 최신성도 확인 (인터넷·키 필요)")
    p.add_argument("--md", help="결과를 Markdown 표로 저장할 경로")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)  # 점검 출력만 보이게 (경고는 점검 결과로 판단)

    if check_db():
        run_scenarios()
    if args.live:
        check_live_apis()
        if hospital_db.available():
            check_freshness()

    failed = [r for r in results if not r["ok"]]
    warned = [r for r in results if r["ok"] and r["warn"]]
    print(f"\n결과: {len(results) - len(failed) - len(warned)}개 통과, {len(warned)}개 주의, {len(failed)}개 실패")
    if args.md:
        write_md(args.md)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
