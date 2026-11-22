"""병원·응급실 검색(최건 담당) 테스트.

기본 테스트는 실제 API를 부르지 않으므로 API 키나 인터넷 없이 실행된다.
    python -X utf8 -m unittest eval.test_hospital_search -v

실제 공공 API까지 확인하려면 (.env에 키 필요, 1~2분 걸림):
    PowerShell:  $env:LIVE_API="1"; python -X utf8 -m unittest eval.test_hospital_search -v
    Git Bash:    LIVE_API=1 python -X utf8 -m unittest eval.test_hospital_search -v
"""
import csv
import io
import logging
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from datetime import date, datetime, timezone
from unittest import mock

import requests

from backend.agent.nodes import search
from backend.api.schemas import HospitalOut
from backend.tools import _common, egen, hira, hospital_db, hours, pharmacy
from backend.tools.hours import HOLIDAY_KEY, KST, Schedule

CITY_HALL = (37.5665, 126.9780)  # 서울시청


def api_response(items, code="00", msg="NORMAL SERVICE."):
    """공공데이터포털 JSON 응답 모양을 흉내 낸 가짜 응답."""
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"response": {
        "header": {"resultCode": code, "resultMsg": msg},
        "body": {"items": items, "totalCount": 0},
    }}
    return resp


def hira_item(name, distance, phone="02-000-0000", addr="서울특별시 중구 세종대로 1", ykiho=None, **extra):
    return {"yadmNm": name, "distance": str(distance), "telno": phone, "addr": addr,
            "ykiho": ykiho or f"Y-{name}", "XPos": "126.97", "YPos": 37.56, **extra}


def er_item(name, lat, lng, tel1="02-111-1111", tel3="02-999-9999"):
    return {"dutyName": name, "wgs84Lat": lat, "wgs84Lon": lng, "dutyTel1": tel1, "dutyTel3": tel3}


def nmc_item(name, tel, **times):
    """병·의원 찾기 API 항목. times 예: d1=("0900", 1800), d8=("0900", 1300)"""
    it = {"dutyName": name, "dutyTel1": tel}
    for key, (s, e) in times.items():
        it[f"dutyTime{key[1:]}s"], it[f"dutyTime{key[1:]}c"] = s, e
    return it


WEEKDAYS = {f"d{d}": ("0900", 1800) for d in range(1, 6)}  # 월~금 09:00~18:00


def kst(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def clear_caches():
    hira._cache.clear()
    egen._cache.update(at=0.0, items=[])
    hours.clear_caches()
    hospital_db._region_cache.clear()


def set_holidays(*days):
    """공휴일 API를 부르지 않도록 캐시에 미리 넣어 둔다."""
    for year in {d.year for d in days} | {2026}:
        hours._holiday_cache[year] = (time.time(), {d for d in days if d.year == year})


class FetchItemsTests(unittest.TestCase):
    """_common.fetch_items: 결과는 항상 list, 0건은 [], 실패는 None."""

    URL = "https://example.test/api"

    @mock.patch("backend.tools._common.requests.get")
    def test_returns_item_list(self, get):
        get.return_value = api_response({"item": [{"a": 1}, {"a": 2}]})
        self.assertEqual(_common.fetch_items(self.URL, "KEY==", {"x": 1}), [{"a": 1}, {"a": 2}])
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["serviceKey"], "KEY==")
        self.assertEqual(params["_type"], "json")
        self.assertEqual(params["x"], 1)

    @mock.patch("backend.tools._common.requests.get")
    def test_single_item_dict_becomes_list(self, get):
        get.return_value = api_response({"item": {"a": 1}})  # 1건이면 dict로 온다
        self.assertEqual(_common.fetch_items(self.URL, "KEY", {}), [{"a": 1}])

    @mock.patch("backend.tools._common.requests.get")
    def test_no_results_is_empty_list(self, get):
        for empty in ("", None, {}, {"item": []}):  # 0건일 때 API가 주는 여러 모양
            with self.subTest(items=empty):
                get.return_value = api_response(empty)
                self.assertEqual(_common.fetch_items(self.URL, "KEY", {}), [])

    @mock.patch("backend.tools._common.requests.get")
    def test_encoding_key_is_decoded(self, get):
        get.return_value = api_response({"item": []})
        _common.fetch_items(self.URL, "abc%2Bdef%3D%3D", {})
        self.assertEqual(get.call_args.kwargs["params"]["serviceKey"], "abc+def==")

    @mock.patch("backend.tools._common.requests.get")
    def test_empty_key_skips_request(self, get):
        with self.assertLogs("backend.tools._common", logging.WARNING):
            self.assertIsNone(_common.fetch_items(self.URL, "", {}))
        get.assert_not_called()

    @mock.patch("backend.tools._common.requests.get")
    def test_error_result_code(self, get):
        get.return_value = api_response(None, code="30", msg="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
        with self.assertLogs("backend.tools._common", logging.WARNING) as logs:
            self.assertIsNone(_common.fetch_items(self.URL, "KEY", {}))
        self.assertIn("30", "".join(logs.output))

    @mock.patch("backend.tools._common.requests.get")
    def test_network_failures_without_leaking_key(self, get):
        secret = "SECRET-KEY-123=="
        for exc in (requests.Timeout(f"timeout ?serviceKey={secret}"),
                    requests.ConnectionError(f"refused ?serviceKey={secret}"),
                    requests.HTTPError(f"403 ?serviceKey={secret}")):
            with self.subTest(exc=type(exc).__name__):
                get.side_effect = exc
                with self.assertLogs("backend.tools._common", logging.WARNING) as logs:
                    self.assertIsNone(_common.fetch_items(self.URL, secret, {}))
                self.assertNotIn(secret, "".join(logs.output))

    @mock.patch("backend.tools._common.requests.get")
    def test_non_json_response(self, get):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")  # 예: XML 오류 페이지
        get.return_value = resp
        with self.assertLogs("backend.tools._common", logging.WARNING):
            self.assertIsNone(_common.fetch_items(self.URL, "KEY", {}))


class HaversineTests(unittest.TestCase):
    def test_known_distance(self):
        # 서울시청 → 강북삼성병원 (E-Gen 응답의 거리 0.91km와 같은 값)
        d = _common.haversine_m(*CITY_HALL, 37.568497631233825, 126.96793805451702)
        self.assertTrue(900 <= d <= 930, d)

    def test_accepts_string_coordinates(self):
        self.assertEqual(_common.haversine_m(*CITY_HALL, "37.5665", "126.9780"), 0)

    def test_missing_or_bad_coordinates(self):
        for lat, lng in [(None, None), ("", ""), ("abc", 1), (0, 0)]:
            with self.subTest(lat=lat, lng=lng):
                self.assertIsNone(_common.haversine_m(*CITY_HALL, lat, lng))


class HiraSearchTests(unittest.TestCase):
    """hira.search_hospitals: 진료과 + 좌표 → 가까운 병원 목록."""

    def setUp(self):
        clear_caches()
        p1 = mock.patch("backend.tools.hira.fetch_items")
        p2 = mock.patch("backend.tools.hours.lookup_schedules", side_effect=lambda recs: [None] * len(recs))
        p3 = mock.patch("backend.tools.hospital_db.available", return_value=False)  # API 경로를 검사
        self.fetch, self.lookup, _ = p1.start(), p2.start(), p3.start()
        for p in (p1, p2, p3):
            self.addCleanup(p.stop)

    def test_sorted_by_distance_and_limited(self):
        # API 결과는 거리순이 아니다(실측) → 직접 정렬해야 한다
        self.fetch.return_value = [hira_item(f"병원{d}", d) for d in (900, 120, 450, 300, 700, 50)]
        out = hira.search_hospitals("정형외과", *CITY_HALL, top=5)
        self.assertEqual([h["distance_m"] for h in out], [50, 120, 300, 450, 700])

    def test_matches_api_contract(self):
        self.fetch.return_value = [hira_item("튼튼정형외과의원", "320.7", phone="02-123-4567")]
        out = hira.search_hospitals("정형외과", *CITY_HALL)
        self.assertEqual(out, [{"name": "튼튼정형외과의원", "dept": "정형외과", "distance_m": 320,
                                "is_open": None, "phone": "02-123-4567", "kind": "병원",
                                "address": "서울특별시 중구 세종대로 1", "lat": 37.56, "lng": 126.97}])
        HospitalOut(**out[0])  # schemas.py 계약 위반이면 예외

    def test_hours_looked_up_once_for_top_results_only(self):
        self.fetch.return_value = [hira_item(f"병원{d}", d) for d in (300, 100, 200, 900)]
        hira.search_hospitals("내과", *CITY_HALL, top=3)
        self.lookup.assert_called_once()  # 병원마다 부르지 않고 한 번에
        records = self.lookup.call_args.args[0]
        self.assertEqual([r["name"] for r in records], ["병원100", "병원200", "병원300"])
        self.assertEqual(set(records[0]), {"name", "phone", "addr", "ykiho"})

    def test_is_open_comes_from_schedule(self):
        open_now, closed = Schedule({1: (0, 2400)}), Schedule({})
        self.lookup.side_effect = lambda recs: [open_now, closed, None][:len(recs)]
        self.fetch.return_value = [hira_item("A", 1), hira_item("B", 2), hira_item("C", 3)]
        verdict = {id(open_now): True, id(closed): False}
        with mock.patch("backend.tools.hours.is_open", side_effect=lambda s: verdict.get(id(s))):
            out = hira.search_hospitals("내과", *CITY_HALL, top=3)
        self.assertEqual([h["is_open"] for h in out], [True, False, None])

    def test_request_params(self):
        self.fetch.return_value = [hira_item("A", 10)] * 5
        hira.search_hospitals("정형외과", *CITY_HALL)
        params = self.fetch.call_args.args[2]
        self.assertEqual(params["dgsbjtCd"], "05")
        self.assertEqual((params["xPos"], params["yPos"]), (CITY_HALL[1], CITY_HALL[0]))  # x=경도, y=위도

    def test_department_codes(self):
        # 실측으로 검증한 코드 (가이드는 산부인과·소아청소년과가 뒤바뀌어 있었음)
        for dept, code in [("내과", "01"), ("정형외과", "05"), ("산부인과", "10"),
                           ("소아청소년과", "11"), ("이비인후과", "13"), ("가정의학과", "23")]:
            with self.subTest(dept=dept):
                self.assertEqual(hira.DGSBJT[dept], code)

    def test_alias_is_normalized(self):
        self.fetch.return_value = [hira_item("A", 10)] * 5
        out = hira.search_hospitals("소아과", *CITY_HALL)
        self.assertEqual(self.fetch.call_args.args[2]["dgsbjtCd"], "11")
        self.assertEqual(out[0]["dept"], "소아청소년과")

    def test_unknown_department(self):
        for dept in ("한의원", "한방내과", "병리과"):  # 한방은 팀 결정으로 제외, 병리과는 환자가 찾아가는 과가 아님
            with self.subTest(dept=dept), self.assertLogs("backend.tools.hira", logging.WARNING):
                self.assertEqual(hira.search_hospitals(dept, *CITY_HALL), [])
        self.fetch.assert_not_called()

    def test_department_resolution(self):
        internal, dental = ("내과", ("01",)), ("치과", hira.DENTAL_CODES)
        for name, expected in [("내과", internal), ("소화기내과", internal), ("순환기내과", internal),
                               ("호흡기내과", internal), ("내분비내과", internal), ("신장내과", internal),
                               ("치과", dental), ("치주과", dental), ("치과교정과", dental),
                               ("구강내과", dental),               # '…내과'지만 치과
                               ("소아과", ("소아청소년과", ("11",))),
                               ("한방내과", ("한방내과", None))]:
            with self.subTest(name=name):
                self.assertEqual(hira.resolve(name), expected)
        self.assertEqual(len(hira.DENTAL_CODES), 12)

    def test_internal_subspecialty_searches_internal_medicine(self):
        # 팀 평가셋 정답에 '소화기내과'·'순환기내과'·'호흡기내과'가 있다
        self.fetch.return_value = [hira_item("A", 10)] * 5
        out = hira.search_hospitals("소화기내과", *CITY_HALL)
        self.assertEqual(self.fetch.call_args.args[2]["dgsbjtCd"], "01")
        self.assertEqual(out[0]["dept"], "내과")

    def test_dental_api_uses_clinic_types_and_merges(self):
        by_type = {"41": [hira_item("치과병원A", 300, ykiho="D1")],
                   "51": [hira_item("치과의원B", 100, ykiho="D2"), hira_item("치과병원A", 300, ykiho="D1")]}
        self.fetch.side_effect = lambda url, key, params, timeout: by_type[params["clCd"]]
        out = hira.search_hospitals("치과", *CITY_HALL, top=2)
        self.assertEqual([h["name"] for h in out], ["치과의원B", "치과병원A"])  # 중복 제거 + 거리순
        sent = [c.args[2] for c in self.fetch.call_args_list]
        self.assertEqual(sorted(p["clCd"] for p in sent), ["41", "51"])
        self.assertTrue(all("dgsbjtCd" not in p for p in sent))

    def test_dental_api_partial_failure(self):
        self.fetch.side_effect = lambda url, key, params, timeout: None if params["clCd"] == "41" else []
        self.assertEqual(hira.search_hospitals("치과", *CITY_HALL), [])
        self.assertEqual(self.fetch.call_count, 2)  # 실패하면 반경을 넓혀 다시 부르지 않음

    def test_missing_location(self):
        self.assertEqual(hira.search_hospitals("내과", None, None), [])
        self.fetch.assert_not_called()

    def test_widens_radius_when_too_few(self):
        # 2km → 5km → 20km (요구사항 FR-21: 기본 2km, 5km까지 넓히고 그래도 없으면 가장 가까운 곳)
        self.assertEqual(hira.RADII, (2000, 5000, 20000))
        self.fetch.side_effect = [[hira_item("가까운곳", 800)],
                                  [hira_item("가까운곳", 800)],
                                  [hira_item("가까운곳", 800), hira_item("먼곳", 15000)]]
        out = hira.search_hospitals("안과", *CITY_HALL, top=2)
        radii = [c.args[2]["radius"] for c in self.fetch.call_args_list]
        self.assertEqual(radii, [2000, 5000, 20000])
        self.assertEqual([h["name"] for h in out], ["가까운곳", "먼곳"])

    def test_stops_at_first_radius_when_enough(self):
        self.fetch.return_value = [hira_item(f"병원{i}", i * 100) for i in range(1, 6)]
        hira.search_hospitals("내과", *CITY_HALL, top=5)
        self.assertEqual(self.fetch.call_count, 1)

    def test_api_failure_does_not_retry_wider(self):
        self.fetch.return_value = None  # 타임아웃 등 실패
        self.assertEqual(hira.search_hospitals("내과", *CITY_HALL), [])
        self.assertEqual(self.fetch.call_count, 1)  # 실패는 '결과 부족'이 아니므로 반경을 넓히지 않음
        self.lookup.assert_not_called()

    def test_distance_fallback_and_missing_coords(self):
        self.fetch.return_value = [
            {"yadmNm": "거리필드없음", "telno": "02", "XPos": "126.9780", "YPos": "37.5665"},
            {"yadmNm": "좌표도없음", "telno": "02"},
        ]
        out = hira.search_hospitals("내과", *CITY_HALL, top=1)
        self.assertEqual([(h["name"], h["distance_m"]) for h in out], [("거리필드없음", 0)])

    def test_empty_phone_becomes_none(self):
        self.fetch.return_value = [hira_item("A", 10, phone="")] * 5
        self.assertIsNone(hira.search_hospitals("내과", *CITY_HALL)[0]["phone"])

    def test_cache_hit_skips_api_but_recomputes_is_open(self):
        self.fetch.return_value = [hira_item(f"병원{i}", i) for i in range(5)]
        with mock.patch("backend.tools.hours.is_open", side_effect=[True] * 5 + [False] * 5):
            # 캐시는 좌표를 소수 3자리(약 100m)로 묶는다. 반올림 경계를 피한 두 점(약 10m 차이)
            first = hira.search_hospitals("내과", 37.5661, 126.9781)
            second = hira.search_hospitals("내과", 37.5662, 126.9782)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.lookup.call_count, 1)
        self.assertTrue(all(h["is_open"] for h in first))       # 진료시간표는 캐시해도
        self.assertFalse(any(h["is_open"] for h in second))     # is_open은 매번 현재 시각으로
        hira.search_hospitals("외과", 37.5661, 126.9781)  # 진료과가 다르면 새로 조회
        self.assertEqual(self.fetch.call_count, 2)

    def test_failure_is_not_cached(self):
        self.fetch.return_value = None  # API 실패
        self.assertEqual(hira.search_hospitals("내과", *CITY_HALL), [])
        self.fetch.return_value = [hira_item(f"병원{i}", i) for i in range(5)]
        self.assertEqual(len(hira.search_hospitals("내과", *CITY_HALL)), 5)


class IsOpenTests(unittest.TestCase):
    """hours.is_open: 진료시간표 + 현재 시각(+ 공휴일) → True/False/None."""

    def setUp(self):
        clear_caches()
        # 2026-11-16(월), 2026-11-17(화)는 평일, 2026-12-25(금)는 공휴일
        set_holidays(date(2026, 12, 25))
        self.weekday = hours._schedule_from_nmc(nmc_item("A", "02", **WEEKDAYS))

    def test_weekday_hours(self):
        for when, expected in [(kst(2026, 11, 16, 10), True),
                               (kst(2026, 11, 16, 8, 59), False),
                               (kst(2026, 11, 16, 9, 0), True),     # 시작 시각 포함
                               (kst(2026, 11, 16, 18, 0), False),   # 종료 시각은 끝난 것
                               (kst(2026, 11, 21, 10), False),      # 토요일: 시간표 없음 → 휴진
                               (kst(2026, 11, 22, 10), False)]:     # 일요일
            with self.subTest(when=when):
                self.assertIs(hours.is_open(self.weekday, when), expected)

    def test_no_schedule_is_unknown(self):
        self.assertIsNone(hours.is_open(None, kst(2026, 11, 16, 10)))

    def test_timezone_converted_to_kst(self):
        utc_1am = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)  # = 한국 10:00
        self.assertTrue(hours.is_open(self.weekday, utc_1am))

    def test_holiday_short_hours(self):
        # 공휴일 단축 진료 (평일 18시까지, 공휴일 13시까지)
        s = hours._schedule_from_nmc(nmc_item("A", "02", **WEEKDAYS, d8=("0900", 1300)))
        self.assertTrue(hours.is_open(s, kst(2026, 12, 25, 12)))
        self.assertFalse(hours.is_open(s, kst(2026, 12, 25, 14)))
        self.assertTrue(hours.is_open(s, kst(2026, 12, 24, 14)))  # 전날(평일)은 평일 시간

    def test_holiday_full_hours(self):
        s = hours._schedule_from_nmc(nmc_item("A", "02", **WEEKDAYS, d8=("0900", 1800)))
        self.assertTrue(hours.is_open(s, kst(2026, 12, 25, 17)))

    def test_holiday_without_hours_is_closed(self):
        # 금요일이라 평일 시간은 있지만, 공휴일 시간이 없으면 휴진
        self.assertFalse(hours.is_open(self.weekday, kst(2026, 12, 25, 10)))

    def test_hira_detail_holiday(self):
        base = {f"trmt{d}Start": "0900" for d in ("Mon", "Tue", "Wed", "Thu", "Fri")}
        base.update({f"trmt{d}End": 1800 for d in ("Mon", "Tue", "Wed", "Thu", "Fri")})
        closed = hours._schedule_from_hira({**base, "noTrmtHoli": "휴진"})
        unknown = hours._schedule_from_hira({**base, "noTrmtHoli": "오전진료"})
        self.assertFalse(hours.is_open(closed, kst(2026, 12, 25, 10)))
        self.assertIsNone(hours.is_open(unknown, kst(2026, 12, 25, 10)))  # 공휴일 진료 여부 모름
        self.assertTrue(hours.is_open(unknown, kst(2026, 11, 16, 10)))    # 평일은 정상 판단

    def test_overnight_hours(self):
        s = Schedule({2: (2200, 600)})  # 화요일 22:00 ~ 수요일 06:00
        self.assertTrue(hours.is_open(s, kst(2026, 11, 17, 23)))   # 화 23시
        self.assertTrue(hours.is_open(s, kst(2026, 11, 18, 3)))    # 수 03시 (전날부터 이어짐)
        self.assertFalse(hours.is_open(s, kst(2026, 11, 18, 7)))   # 수 07시
        self.assertFalse(hours.is_open(s, kst(2026, 11, 17, 21)))  # 화 21시

    def test_time_parsing(self):
        for raw, expected in [("0930", 930), (1830, 1830), ("2400", 2400), (" 900 ", 900),
                              ("2460", None), ("2500", None), ("abc", None), (None, None), ("", None)]:
            with self.subTest(raw=raw):
                self.assertEqual(hours._hhmm(raw), expected)

    def test_nmc_without_any_hours(self):
        self.assertIsNone(hours._schedule_from_nmc(nmc_item("시간없음", "02")))

    def test_zero_to_zero_means_closed(self):
        # 분기 파일·상세정보는 쉬는 요일을 '0'~'0'으로 적는다 (24시간 진료로 오해하면 안 됨)
        s = hours._schedule_from_hira({"trmtMonStart": "900", "trmtMonEnd": "1800",
                                       "trmtSunStart": "0", "trmtSunEnd": "0"})
        self.assertEqual(set(s.days), {1})
        self.assertFalse(hours.is_open(s, kst(2026, 11, 22, 12)))  # 일요일

    def test_lunch_break(self):
        s = hours._schedule_from_hira({"trmtMonStart": "900", "trmtMonEnd": "1800",
                                       "trmtSatStart": "900", "trmtSatEnd": "1300",
                                       "lunchWeek": "13시00분~14시00분", "lunchSat": "진료없음"})
        self.assertTrue(hours.is_open(s, kst(2026, 11, 16, 12, 59)))
        self.assertFalse(hours.is_open(s, kst(2026, 11, 16, 13, 30)))  # 점심시간
        self.assertTrue(hours.is_open(s, kst(2026, 11, 16, 14, 0)))
        self.assertNotIn(6, s.lunch)  # 토요일 '진료없음'은 점심시간이 아님

    def test_lunch_parsing(self):
        for text, expected in [("13시00분~14시00분", (1300, 1400)), ("12:30 ~ 13:30", (1230, 1330)),
                               ("13:00 -14:00", (1300, 1400)), ("13시~14시", (1300, 1400)),
                               ("1시~2시", (1300, 1400)), ("12시30분~1시30분", (1230, 1330)),
                               ("진료없음", None), ("", None), (None, None),
                               ("09:00~18:00", None)]:  # 점심시간으로 보기 어려운 값
            with self.subTest(text=text):
                self.assertEqual(hours._parse_lunch(text), expected)

    def test_holiday_text_partial_closing_is_unknown(self):
        base = {"trmtFriStart": "900", "trmtFriEnd": "1800"}
        for text, known in [("전부 휴진", True), ("휴진", True), ("종일휴진", True), ("휴무", True),
                            ("오후 2시 이후 휴진", False), ("오전 10시 진료 시작, 오후 2시 이후 휴진", False),
                            ("진료", False), ("", False)]:
            with self.subTest(text=text):
                s = hours._schedule_from_hira({**base, "noTrmtHoli": text})
                self.assertIs(s.holiday_known, known)

    def test_schedule_json_round_trip(self):
        s = Schedule({1: (900, 1800), 8: (900, 1300)}, holiday_known=False, lunch={1: (1300, 1400)})
        self.assertEqual(Schedule.from_json(s.to_json()), s)
        self.assertIsNone(Schedule.from_json(None))


class HolidayTests(unittest.TestCase):
    """병원 DB가 없을 때 공휴일 API 경로. (DB 경로는 PlacesDbTests)"""

    def setUp(self):
        clear_caches()
        p1 = mock.patch("backend.tools.hours.fetch_items")
        p2 = mock.patch("backend.tools.hospital_db.available", return_value=False)
        self.fetch, _ = p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_parses_and_caches(self):
        self.fetch.return_value = [
            {"locdate": 20261225, "dateName": "기독탄신일", "isHoliday": "Y"},
            {"locdate": 20260303, "dateName": "임시공휴일", "isHoliday": "Y"},
            {"locdate": 20260101, "dateName": "기념일", "isHoliday": "N"},
        ]
        self.assertTrue(hours.is_holiday(date(2026, 12, 25)))
        self.assertTrue(hours.is_holiday(date(2026, 3, 3)))   # 임시공휴일 포함
        self.assertFalse(hours.is_holiday(date(2026, 1, 1)))  # isHoliday=N
        self.assertFalse(hours.is_holiday(date(2026, 11, 16)))
        self.assertEqual(self.fetch.call_count, 1)  # 1년치를 한 번만
        self.assertEqual(self.fetch.call_args.args[2]["solYear"], 2026)

    def test_failure_treated_as_weekday_and_retried(self):
        self.fetch.return_value = None
        with self.assertLogs("backend.tools.hours", logging.WARNING):
            self.assertFalse(hours.is_holiday(date(2026, 12, 25)))
        self.fetch.return_value = [{"locdate": 20261225, "isHoliday": "Y"}]
        self.assertTrue(hours.is_holiday(date(2026, 12, 25)))


class LookupSchedulesTests(unittest.TestCase):
    """hours.lookup_schedules: 병원 N곳의 진료시간을 몇 번의 호출로 찾는지."""

    JUNG = "서울특별시 중구 세종대로 1"
    JONGNO = "서울특별시 종로구 종로 1"

    def setUp(self):
        clear_caches()
        self.regions = {
            ("서울특별시", "중구"): [nmc_item("중구A의원", "02-111-1111", **WEEKDAYS),
                                    nmc_item("중구B의원", "02-222-2222", **WEEKDAYS)],
            ("서울특별시", "종로구"): [nmc_item("종로C의원", "02-333-3333", **WEEKDAYS)],
        }
        self.details = {"Y-없는병원": [{"trmtMonStart": "0900", "trmtMonEnd": 1800}], "Y-상세도없음": []}
        p = mock.patch("backend.tools.hours.fetch_items", side_effect=self._fake_fetch)
        self.fetch = p.start()
        self.addCleanup(p.stop)

    def _fake_fetch(self, url, key, params, timeout=5):
        if url == hours.NMC_LIST:
            return self.regions.get((params["Q0"], params["Q1"]), [])
        if url == hours.HIRA_DETAIL:
            return self.details.get(params["ykiho"], [])
        raise AssertionError(url)

    def calls(self, url):
        return [c for c in self.fetch.call_args_list if c.args[0] == url]

    def rec(self, name, phone, addr, ykiho=None):
        return {"name": name, "phone": phone, "addr": addr, "ykiho": ykiho or f"Y-{name}"}

    def test_one_call_per_region_not_per_hospital(self):
        recs = [self.rec("중구A의원", "02-111-1111", self.JUNG),
                self.rec("중구B의원", "02-222-2222", self.JUNG),
                self.rec("종로C의원", "02-333-3333", self.JONGNO),
                self.rec("중구A의원", "02-111-1111", self.JUNG)]
        out = hours.lookup_schedules(recs)
        self.assertTrue(all(out))
        self.assertEqual(len(self.calls(hours.NMC_LIST)), 2)    # 병원 4곳, 시군구 2개 → 2번
        self.assertEqual(len(self.calls(hours.HIRA_DETAIL)), 0)  # 다 찾았으니 상세조회 없음

    def test_detail_only_for_unmatched(self):
        recs = [self.rec("중구A의원", "02-111-1111", self.JUNG),
                self.rec("없는병원", "02-999-0000", self.JUNG),
                self.rec("상세도없음", "02-999-1111", self.JUNG)]
        out = hours.lookup_schedules(recs)
        self.assertIsNotNone(out[0])
        self.assertEqual(out[1].days, {1: (900, 1800)})  # 2단계(심평원 상세)에서 찾음
        self.assertIsNone(out[2])                         # 둘 다 없으면 정보 없음
        detail_ids = sorted(c.args[2]["ykiho"] for c in self.calls(hours.HIRA_DETAIL))
        self.assertEqual(detail_ids, sorted(["Y-없는병원", "Y-상세도없음"]))  # 찾은 병원(중구A)은 조회 안 함

    def test_second_lookup_uses_cache(self):
        recs = [self.rec("중구A의원", "02-111-1111", self.JUNG),
                self.rec("상세도없음", "02-999-1111", self.JUNG)]
        hours.lookup_schedules(recs)
        before = self.fetch.call_count
        hours.lookup_schedules(recs)
        self.assertEqual(self.fetch.call_count, before)  # '정보 없음'도 캐시되어 재호출 없음

    def test_matching_rules(self):
        index = hours._region_index(("서울특별시", "중구"))
        self.assertEqual(hours._match(index, "다른이름", "02)111-1111")["dutyName"], "중구A의원")  # 전화번호 형식 무관
        self.assertEqual(hours._match(index, "중구B의원", None)["dutyName"], "중구B의원")         # 전화 없으면 이름
        self.assertIsNone(hours._match(index, "모르는곳", "02-000-0000"))

    def test_ambiguous_name_is_not_guessed(self):
        self.regions[("서울특별시", "중구")] += [nmc_item("같은이름의원", "02-500-0001", **WEEKDAYS),
                                                nmc_item("같은이름의원", "02-500-0002", **WEEKDAYS)]
        index = hours._region_index(("서울특별시", "중구"))
        self.assertIsNone(hours._match(index, "같은이름의원", "02-777-7777"))
        self.assertIsNotNone(hours._match(index, "같은이름의원", "02-500-0002"))

    def test_region_failure_falls_back_and_is_retried(self):
        self.regions = {}
        self.fetch.side_effect = lambda url, key, params, timeout=5: (
            None if url == hours.NMC_LIST else self._fake_fetch(url, key, params))
        out = hours.lookup_schedules([self.rec("없는병원", "02", self.JUNG)])
        self.assertIsNotNone(out[0])  # 1단계 실패 → 2단계로 찾음
        self.assertNotIn(("서울특별시", "중구"), hours._region_cache)  # 실패는 캐시 안 함

    def test_region_from_address(self):
        for addr, expected in [("서울특별시 중구 세종대로 1", ("서울특별시", "중구")),
                               ("경기도 수원시 권선구 효원로 1", ("경기도", "수원시")),
                               ("경기도 양평군 양평읍 1", ("경기도", "양평군")),
                               ("세종특별자치시 새롬중앙로 55", ("세종특별자치시", "")),
                               ("", None), (None, None)]:
            with self.subTest(addr=addr):
                self.assertEqual(hours._region(addr), expected)


def write_fixture_zip(path):
    """실제 분기 파일과 같은 칸 이름·인코딩(cp949)의 작은 zip을 만든다."""
    def csv_bytes(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue().encode("cp949")

    hosp_header = ["암호화요양기호", "요양기관명", "종별코드명", "주소", "전화번호", "좌표(X)", "좌표(Y)"]
    hosp = [  # 서울시청(37.5665, 126.9780) 기준
        ["Y1", "가까운정형외과의원", "의원", "서울특별시 중구 세종대로 1", "02-111-1111", "126.9790", "37.5670"],
        ["Y2", "조금먼정형외과의원", "의원", "서울특별시 중구 세종대로 2", "02-222-2222", "126.9900", "37.5700"],
        ["Y3", "상세만있는의원", "의원", "서울특별시 종로구 종로 1", "02-333-3333", "126.9800", "37.5680"],
        ["Y4", "시간없는의원", "의원", "서울특별시 종로구 종로 2", "02-444-4444", "126.9810", "37.5690"],
        ["Y5", "먼내과의원", "의원", "서울특별시 강남구 강남대로 1", "02-555-5555", "127.0276", "37.4979"],
        ["Y6", "좌표없는의원", "의원", "서울특별시 중구 세종대로 3", "02-666-6666", "", ""],
        ["Y7", "동네치과의원", "치과의원", "서울특별시 중구 세종대로 4", "02-777-7777", "126.9785", "37.5668"],
        ["Y8", "부산중구의원", "의원", "부산광역시 중구 중앙대로 1", "051-888-8888", "129.0320", "35.1030"],
    ]
    days = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"]
    detail_header = ["암호화요양기호", "요양기관명", "휴진안내_공휴일", "점심시간_평일", "점심시간_토요일"]
    detail_header += [f"진료{k}시간_{d}" for d in days for k in ("시작", "종료")]
    weekday_hours = ["0", "0"] + ["900", "1800"] * 5 + ["900", "1300"]
    detail = [["Y1", "가까운정형외과의원", "전부 휴진", "13시00분~14시00분", ""] + weekday_hours,
              ["Y3", "상세만있는의원", "휴진", "12:30~13:30", ""] + weekday_hours]
    subj = [["Y1", "05"], ["Y2", "05"], ["Y3", "05"], ["Y4", "05"], ["Y5", "01"], ["Y6", "05"], ["Y1", "01"],
            ["Y7", "51"], ["Y7", "54"], ["Y7", "56"],  # 치과의원은 '49 치과' 대신 세부 코드 여러 개
            ["Y8", "13"]]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("1.병원정보서비스(2026.9.).csv", csv_bytes(hosp_header, hosp))
        z.writestr("4.의료기관별상세정보서비스_02_세부정보(2026.9.).csv", csv_bytes(detail_header, detail))
        z.writestr("5.의료기관별상세정보서비스_03_진료과목정보(2026.9.).csv",
                   csv_bytes(["암호화요양기호", "진료과목코드"], subj))


FIXTURE_NMC = [  # 병·의원 찾기: Y1·Y2만 연결된다 (Y1은 전화번호 형식이 달라도 연결)
    {**nmc_item("가까운정형외과의원", "021111111", **WEEKDAYS, d8=("0900", 1300)), "dutyAddr": "서울특별시 중구 세종대로 1"},
    {**nmc_item("조금먼정형외과의원", "02-222-2222", **WEEKDAYS), "dutyAddr": "서울특별시 중구 세종대로 2"},
    {**nmc_item("다른동네의원", "02-999-9999", **WEEKDAYS), "dutyAddr": "부산광역시 중구 중앙대로 1"},
]
FIXTURE_PHARMACIES = [  # 약국: 국립중앙의료원 약국 목록 모양
    {**nmc_item("시청앞약국", "02-100-0001", **WEEKDAYS, d8=("0900", 1300)), "hpid": "C1",
     "dutyAddr": "서울특별시 중구 세종대로 10", "wgs84Lat": 37.5667, "wgs84Lon": 126.9782},
    {"dutyName": "시간모름약국", "dutyTel1": "02-100-0002", "hpid": "C2",
     "dutyAddr": "서울특별시 중구 세종대로 11", "wgs84Lat": 37.5690, "wgs84Lon": 126.9800},
    {"dutyName": "좌표없는약국", "hpid": "C3", "dutyAddr": "서울특별시 중구 세종대로 12"},
]
FIXTURE_ERS = [  # 응급실: E-Gen 응급의료기관 목록 모양
    {**er_item("먼응급실", 37.6000, 127.0000), "hpid": "E1", "dutyAddr": "서울특별시 성북구 1"},
    {**er_item("가까운응급실", 37.5680, 126.9790, tel1="02-대표", tel3="02-응급실직통"), "hpid": "E2",
     "dutyAddr": "서울특별시 중구 2"},
]
FIXTURE_HOLIDAYS = {2026: {date(2026, 12, 25), date(2026, 10, 5)}}


class HospitalDbTests(unittest.TestCase):
    """hospital_db: 분기 파일 + 병·의원 찾기 → SQLite, 그리고 DB 검색."""

    def setUp(self):
        clear_caches()
        set_holidays(date(2026, 12, 25))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.zip_path = os.path.join(tmp.name, "fixture.zip")
        self.db_path = os.path.join(tmp.name, "hospitals.sqlite")
        write_fixture_zip(self.zip_path)
        self.stats = hospital_db.build(self.zip_path, FIXTURE_NMC, self.db_path, version="테스트_20260930",
                                       pharmacies=FIXTURE_PHARMACIES, ers=FIXTURE_ERS,
                                       holiday_days=FIXTURE_HOLIDAYS)
        p = mock.patch.object(hospital_db, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)

    def test_build_stats(self):
        self.assertEqual(self.stats["hospitals"], 7)   # 좌표 없는 1곳 제외
        self.assertEqual(self.stats["no_coords"], 2)   # 병원 1 + 약국 1
        self.assertEqual(self.stats["nmc"], 2)         # Y1, Y2
        self.assertEqual(self.stats["hira"], 1)        # Y3 (병·의원 찾기에 없음 → 심평원 세부정보)
        self.assertEqual(self.stats["none"], 4)        # Y4, Y5, Y7, Y8
        self.assertEqual((self.stats["pharmacies"], self.stats["ers"], self.stats["holidays"]), (2, 2, 2))
        meta = hospital_db.info()
        self.assertEqual(meta["hira_version"], "테스트_20260930")
        self.assertEqual(meta["schema_version"], hospital_db.SCHEMA_VERSION)
        self.assertTrue(hospital_db.available())
        self.assertFalse(os.path.exists(self.db_path + ".tmp"))

    def test_search_filters_department_and_radius_sorted(self):
        out = hospital_db.search("05", *CITY_HALL, radius_m=2000, limit=10)
        self.assertEqual([h["name"] for h, _ in out], ["가까운정형외과의원", "상세만있는의원", "시간없는의원", "조금먼정형외과의원"])
        self.assertEqual([h["name"] for h, _ in hospital_db.search("01", *CITY_HALL, 2000, 10)], ["가까운정형외과의원"])
        far = hospital_db.search("01", *CITY_HALL, 20000, 10)
        self.assertEqual([h["name"] for h, _ in far], ["가까운정형외과의원", "먼내과의원"])

    def test_schedule_sources_are_merged(self):
        out = {h["name"]: s for h, s in hospital_db.search("05", *CITY_HALL, 2000, 10)}
        merged = out["가까운정형외과의원"]
        self.assertEqual(merged.days[HOLIDAY_KEY], (900, 1300))   # 공휴일 시간은 병·의원 찾기에서
        self.assertEqual(merged.lunch[1], (1300, 1400))           # 점심시간은 심평원 세부정보에서
        self.assertTrue(out["상세만있는의원"].holiday_known)        # '휴진' → 공휴일 휴진 확실
        self.assertEqual(out["상세만있는의원"].lunch[1], (1230, 1330))
        self.assertIsNone(out["시간없는의원"])

    def test_hira_uses_db_without_any_api_call(self):
        with mock.patch("backend.tools.hira.fetch_items") as api, mock.patch("backend.tools.hours.fetch_items") as api2:
            with mock.patch("backend.tools.hours.datetime") as dt:
                dt.now.return_value = kst(2026, 11, 16, 13, 15)  # 월요일, 두 병원 모두 점심시간
                out = hira.search_hospitals("정형외과", *CITY_HALL, top=3)
        api.assert_not_called()
        api2.assert_not_called()
        self.assertEqual([(h["name"], h["dept"], h["is_open"]) for h in out],
                         [("가까운정형외과의원", "정형외과", False),   # 점심시간
                          ("상세만있는의원", "정형외과", False),       # 12:30~13:30 점심시간
                          ("시간없는의원", "정형외과", None)])
        for h in out:
            HospitalOut(**h)

    def _set_specialists(self, pid, code, n):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("UPDATE subject SET specialists = ? WHERE id = ? AND code = ?", (n, pid, code))
            con.commit()
        finally:
            con.close()

    def test_specialists_default_zero_when_column_missing(self):
        # 픽스처 zip에는 '과목별 전문의수' 칸이 없다 → 0으로 저장
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM subject WHERE specialists IS NULL OR specialists != 0")
                             .fetchone()[0], 0)
        finally:
            con.close()

    def test_specialist_first_then_distance(self):
        # Y2(조금먼정형외과의원)만 정형외과 전문의가 있으면, 더 가까운 곳들보다 먼저 나온다
        self._set_specialists("Y2", "05", 2)
        out = hira.search_hospitals("정형외과", *CITY_HALL, top=3)
        self.assertEqual([h["name"] for h in out], ["조금먼정형외과의원", "가까운정형외과의원", "상세만있는의원"])
        # 요청하지 않으면 거리순 그대로
        plain = hospital_db.search("05", *CITY_HALL, radius_m=2000, limit=3)
        self.assertEqual([h["name"] for h, _ in plain], ["가까운정형외과의원", "상세만있는의원", "시간없는의원"])

    def test_dental_stays_distance_order(self):
        with mock.patch.object(hospital_db, "search", wraps=hospital_db.search) as spy:
            hira.search_hospitals("치과", *CITY_HALL)
        self.assertFalse(spy.call_args.kwargs["prefer_specialist"])
        hira.search_hospitals("정형외과", *CITY_HALL)

    def test_dental_matches_any_dental_code_once(self):
        out = hospital_db.search(hira.DENTAL_CODES, *CITY_HALL, 2000, 10)
        self.assertEqual([h["name"] for h, _ in out], ["동네치과의원"])  # 코드 3개에 걸려도 한 번만
        self.assertEqual([h["name"] for h, _ in hospital_db.search("49", *CITY_HALL, 2000, 10)], [])
        found = hira.search_hospitals("치주과", *CITY_HALL)
        self.assertEqual([(h["name"], h["dept"]) for h in found], [("동네치과의원", "치과")])

    def test_hira_widens_radius_in_db(self):
        out = hira.search_hospitals("내과", *CITY_HALL, top=2)
        self.assertEqual([h["name"] for h in out], ["가까운정형외과의원", "먼내과의원"])  # 2km → 20km

    def test_broken_db_falls_back_to_api(self):
        with open(self.db_path, "wb") as f:
            f.write(b"not a database")
        with mock.patch("backend.tools.hira.fetch_items", return_value=None) as api, \
                self.assertLogs("backend.tools.hospital_db", logging.WARNING) as logs:
            self.assertEqual(hira.search_hospitals("정형외과", *CITY_HALL), [])
        api.assert_called()  # DB를 못 쓰면 API로 대신 찾는다
        self.assertIn("다시 실행", "".join(logs.output))

    def test_zip_without_expected_file(self):
        bad = os.path.join(os.path.dirname(self.zip_path), "bad.zip")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("other.csv", b"a,b\n")
        with self.assertRaises(ValueError):
            hospital_db.build(bad, [], self.db_path + "2")


class FetchNmcAllTests(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch("backend.tools.hospital_db.fetch_items")
        p2 = mock.patch("backend.tools.hospital_db.time.sleep")  # 재시도 대기 생략
        self.fetch, _ = p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    @mock.patch.object(hospital_db, "PAGE_ROWS", 2)
    def test_pages_until_short_page_with_retry(self):
        self.fetch.side_effect = [[{"a": 1}, {"a": 2}], None, [{"a": 3}, {"a": 4}], [{"a": 5}]]
        self.assertEqual(len(hospital_db.fetch_nmc_all()), 5)
        self.assertEqual([c.args[2]["pageNo"] for c in self.fetch.call_args_list], [1, 2, 2, 3])

    @mock.patch.object(hospital_db, "PAGE_ROWS", 2)
    def test_gives_up_after_three_failures(self):
        self.fetch.side_effect = [[{"a": 1}, {"a": 2}], None, None, None]
        with self.assertRaises(RuntimeError):
            hospital_db.fetch_nmc_all()

    def test_each_list_uses_its_own_api(self):
        self.fetch.return_value = []
        hospital_db.fetch_pharmacies_all()
        hospital_db.fetch_ers_all()
        self.assertEqual([c.args[0] for c in self.fetch.call_args_list], [hospital_db.PHARMACY_LIST, egen.ER_LIST])


class PlacesDbTests(unittest.TestCase):
    """병원 DB의 약국·응급실·공휴일·지역명 검색 — 요청 때 API를 부르지 않아야 한다(요구사항 IF-08)."""

    def setUp(self):
        HospitalDbTests.setUp(self)
        hours.clear_caches()  # 공휴일도 DB에서 읽는지 보려고 미리 넣어 둔 값 제거
        patches = [mock.patch(f"backend.tools.{m}.fetch_items", side_effect=AssertionError("API 호출 금지"))
                   for m in ("hira", "hours", "egen", "pharmacy")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def at(self, when):
        p = mock.patch("backend.tools.hours.datetime")
        dt = p.start()
        self.addCleanup(p.stop)
        dt.now.return_value = when

    def test_pharmacies_with_opening_hours(self):
        self.at(kst(2026, 11, 16, 10))  # 월요일 오전
        out = pharmacy.search_pharmacies(*CITY_HALL)
        self.assertEqual([(p["name"], p["kind"], p["dept"], p["is_open"]) for p in out],
                         [("시청앞약국", "약국", "약국", True), ("시간모름약국", "약국", "약국", None)])
        self.assertEqual(out[0]["address"], "서울특별시 중구 세종대로 10")
        for p in out:
            HospitalOut(**p)

    def test_pharmacy_holiday_hours_from_db(self):
        self.at(kst(2026, 12, 25, 12))  # 공휴일(DB에 저장) 단축 운영 09~13시
        self.assertTrue(pharmacy.search_pharmacies(*CITY_HALL, top=1)[0]["is_open"])
        hours.clear_caches()
        self.at(kst(2026, 12, 25, 14))
        self.assertFalse(pharmacy.search_pharmacies(*CITY_HALL, top=1)[0]["is_open"])

    def test_emergency_rooms_from_db(self):
        out = egen.search_emergency(*CITY_HALL)
        self.assertEqual([(e["name"], e["phone"], e["kind"], e["is_open"]) for e in out],
                         [("가까운응급실", "02-응급실직통", "응급실", True), ("먼응급실", "02-999-9999", "응급실", True)])
        self.assertEqual(out[0]["dept"], "응급의학과")

    def test_holidays_read_from_db(self):
        self.assertEqual(hours.holidays_of(2026), FIXTURE_HOLIDAYS[2026])
        self.assertIsNone(hospital_db.holidays(2030))  # 저장 안 된 해는 None → API로 받음

    def test_hospital_results_have_new_fields(self):
        h = hira.search_hospitals("정형외과", *CITY_HALL, top=1)[0]
        self.assertEqual((h["kind"], h["address"], h["lat"], h["lng"]),
                         ("병원", "서울특별시 중구 세종대로 1", 37.5670, 126.9790))

    def test_locate_region(self):
        lat, lng = hospital_db.locate("서울 중구")
        self.assertAlmostEqual(lat, 37.5670, places=3)
        self.assertAlmostEqual(hospital_db.locate("강남구")[0], 37.4979, places=3)
        self.assertIsNotNone(hospital_db.locate("서울특별시, 종로구"))  # 쉼표·전체 이름도 됨
        self.assertIsNone(hospital_db.locate("중구"))         # 서울 중구와 부산 중구 → 애매하면 모름
        for region in ("없는동네", "", None, "100%_할인"):
            with self.subTest(region=region):
                self.assertIsNone(hospital_db.locate(region))

    def test_resolve_region_ok(self):
        r = hospital_db.resolve_region("서울 중구")
        self.assertEqual((r["status"], r["name"], r["candidates"]), ("ok", "서울특별시 중구", []))
        self.assertEqual(hospital_db.resolve_region("종로")["name"], "서울특별시 종로구")  # 줄임말도 됨

    def test_resolve_region_ambiguous_with_candidates(self):
        # 요구사항 FR-29·FR-30: '중구'는 서울·부산 두 곳 → 위치를 정하지 않고 후보를 준다
        r = hospital_db.resolve_region("중구")
        self.assertEqual((r["status"], r["point"]), ("ambiguous", None))
        self.assertEqual([c["name"] for c in r["candidates"]], ["서울특별시 중구", "부산광역시 중구"])  # 병원 많은 순
        self.assertIsNone(r["candidates"][0]["distance_m"])
        busan_gps = (35.18, 129.07)
        near = hospital_db.resolve_region("중구", near=busan_gps)["candidates"]
        self.assertEqual(near[0]["name"], "부산광역시 중구")  # GPS가 있으면 가까운 순
        self.assertLess(near[0]["distance_m"], near[1]["distance_m"])

    def test_resolve_region_ignores_road_names(self):
        # 서울 중구 '세종대로'는 도로명이라 '세종'에 걸리지 않아야 한다
        self.assertEqual(hospital_db.resolve_region("세종")["status"], "not_found")
        self.assertEqual(hospital_db.resolve_region("없는동네")["status"], "not_found")

    def test_region_index_is_built_and_used(self):
        con = sqlite3.connect(self.db_path)
        try:
            self.assertGreater(con.execute("SELECT COUNT(*) FROM area").fetchone()[0], 0)
        finally:
            con.close()
        hospital_db._region_cache.clear()
        # 색인이 있으면 주소를 다시 해석하지 않는다 (8만 건을 훑지 않음)
        with mock.patch.object(hospital_db, "_admin_units", side_effect=AssertionError("주소 재해석 금지")):
            self.assertEqual(hospital_db.resolve_region("서울 중구")["status"], "ok")

    def test_region_result_is_cached_but_distance_follows_gps(self):
        hospital_db._region_cache.clear()
        hospital_db.resolve_region("중구")
        # 수집 단계와 검색 단계가 같은 지역명을 다시 물어도 DB를 다시 열지 않는다
        with mock.patch.object(hospital_db, "_connect", side_effect=AssertionError("캐시를 써야 함")):
            busan = hospital_db.resolve_region("중구", near=(35.18, 129.07))
            seoul = hospital_db.resolve_region("중구", near=CITY_HALL)
        self.assertEqual(busan["candidates"][0]["name"], "부산광역시 중구")
        self.assertEqual(seoul["candidates"][0]["name"], "서울특별시 중구")

    def test_empty_index_falls_back_to_address_scan(self):
        # place 표에 직접 넣어 만든 DB(그래프 통합 테스트용 등)도 지역명을 찾을 수 있어야 한다
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("DELETE FROM area")
            con.commit()
        finally:
            con.close()
        hospital_db._region_cache.clear()
        self.assertEqual(hospital_db.resolve_region("서울 중구")["status"], "ok")
        self.assertEqual(hospital_db.resolve_region("중구")["status"], "ambiguous")

    def test_admin_units(self):
        for addr, expected in [
            ("서울특별시 중구 세종대로 124, 3층 (태평로1가)", ["서울특별시", "중구", "태평로1가"]),
            ("경기도 수원시 권선구 효원로256번길 7 (권선동)", ["경기도", "수원시", "권선구", "권선동"]),
            ("세종특별자치시 새롬중앙로 55, 3층 (새롬동, 행운빌딩)", ["세종특별자치시", "새롬동"]),
            ("경기도 여주시 세종대왕면 영릉로 1", ["경기도", "여주시", "세종대왕면"]),
            ("", [])]:
            with self.subTest(addr=addr):
                self.assertEqual(hospital_db._admin_units(addr), expected)

    def test_old_format_db_is_not_used(self):
        with mock.patch.object(hospital_db, "SCHEMA_VERSION", "옛날"), \
                self.assertLogs("backend.tools.hospital_db", logging.WARNING):
            hospital_db._checked["key"] = None
            self.assertFalse(hospital_db.available())
        hospital_db._checked["key"] = None
        self.assertTrue(hospital_db.available())


class PharmacyApiTests(unittest.TestCase):
    """병원 DB가 없을 때의 약국 API 경로."""

    def setUp(self):
        clear_caches()
        pharmacy._cache.clear()
        p1 = mock.patch("backend.tools.pharmacy.fetch_items")
        p2 = mock.patch("backend.tools.hospital_db.available", return_value=False)
        self.fetch, _ = p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_nearest_first_without_opening_status(self):
        self.fetch.return_value = [
            {"dutyName": "먼약국", "latitude": 37.5700, "longitude": 126.9900, "dutyTel1": "02-1", "dutyAddr": "A"},
            {"dutyName": "가까운약국", "latitude": 37.5667, "longitude": 126.9782, "dutyTel1": "02-2", "dutyAddr": "B"},
            {"dutyName": "좌표없음"},
        ]
        out = pharmacy.search_pharmacies(*CITY_HALL)
        self.assertEqual([(p["name"], p["is_open"], p["kind"]) for p in out],
                         [("가까운약국", None, "약국"), ("먼약국", None, "약국")])
        params = self.fetch.call_args.args[2]
        self.assertEqual((params["WGS84_LAT"], params["WGS84_LON"]), CITY_HALL)
        pharmacy.search_pharmacies(*CITY_HALL)
        self.assertEqual(self.fetch.call_count, 1)  # 같은 위치는 캐시

    def test_failure_and_missing_location(self):
        self.fetch.return_value = None
        self.assertEqual(pharmacy.search_pharmacies(*CITY_HALL), [])
        self.assertEqual(pharmacy.search_pharmacies(None, None), [])


class EgenSearchTests(unittest.TestCase):
    """egen.search_emergency: 좌표 → 가까운 응급실 목록 (병원 DB가 없을 때의 API 경로)."""

    def setUp(self):
        clear_caches()
        p1 = mock.patch("backend.tools.egen.fetch_items")
        p2 = mock.patch("backend.tools.hospital_db.available", return_value=False)
        self.fetch, _ = p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_nearest_first(self):
        self.fetch.return_value = [
            er_item("먼병원", 37.60, 127.00),
            er_item("가까운병원", 37.5670, 126.9785),
            er_item("중간병원", 37.58, 126.98),
            er_item("부산병원", 35.18, 129.07),
        ]
        out = egen.search_emergency(*CITY_HALL, top=3)
        self.assertEqual([h["name"] for h in out], ["가까운병원", "중간병원", "먼병원"])
        for h in out:
            HospitalOut(**h)
            self.assertEqual(h["dept"], "응급의학과")
            self.assertTrue(h["is_open"])

    def test_prefers_er_direct_line(self):
        self.fetch.return_value = [er_item("A", 37.5670, 126.9785, tel1="02-대표", tel3="02-응급실"),
                                   er_item("B", 37.5680, 126.9785, tel1="02-대표B", tel3=None)]
        out = egen.search_emergency(*CITY_HALL)
        self.assertEqual([h["phone"] for h in out], ["02-응급실", "02-대표B"])

    def test_skips_items_without_coordinates(self):
        self.fetch.return_value = [er_item("좌표없음", None, None), er_item("정상", 37.5670, 126.9785)]
        self.assertEqual([h["name"] for h in egen.search_emergency(*CITY_HALL)], ["정상"])

    def test_missing_location(self):
        self.assertEqual(egen.search_emergency(None, None), [])
        self.fetch.assert_not_called()

    def test_list_is_cached(self):
        self.fetch.return_value = [er_item("A", 37.5670, 126.9785)]
        egen.search_emergency(*CITY_HALL)
        egen.search_emergency(35.18, 129.07)  # 다른 위치라도 전국 목록은 재사용
        self.assertEqual(self.fetch.call_count, 1)

    def test_failure_is_not_cached(self):
        self.fetch.return_value = None
        self.assertEqual(egen.search_emergency(*CITY_HALL), [])
        self.fetch.return_value = [er_item("A", 37.5670, 126.9785)]
        self.assertEqual(len(egen.search_emergency(*CITY_HALL)), 1)


class SearchNodeTests(unittest.TestCase):
    """④ search 노드: State 규약대로 hospitals 칸만 돌려준다."""

    STATE = {"user_input": "무릎이 아파요",
             "departments": [{"name": "정형외과", "score": 0.9, "reason": "무릎 통증"},
                             {"name": "재활의학과", "score": 0.5, "reason": "보조"}],
             "location": {"lat": CITY_HALL[0], "lng": CITY_HALL[1]}}

    def setUp(self):
        self.hira = self._patch("backend.tools.hira.search_hospitals", [{"name": "병원", "kind": "병원"}])
        self.drug = self._patch("backend.tools.pharmacy.search_pharmacies", [{"name": "약국", "kind": "약국"}])
        self.er = self._patch("backend.tools.egen.search_emergency", [{"name": "응급실", "kind": "응급실"}])

    def _patch(self, target, result):
        p = mock.patch(target, return_value=result)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_emergency_branch_returns_only_er(self):
        # 요구사항 FR-08·IF-03: 응급이면 일반 병원 검색 없이 가까운 응급실만
        out = search.search_emergency_rooms(dict(self.STATE))
        self.assertEqual(out, {"hospitals": [{"name": "응급실", "kind": "응급실"}]})
        self.er.assert_called_once_with(*CITY_HALL)
        self.hira.assert_not_called()
        self.drug.assert_not_called()
        self.assertEqual(search.search_emergency_rooms({"location": {}}), {"hospitals": []})

    def test_returns_only_hospitals_field(self):
        state = dict(self.STATE)
        out = search.search_hospitals(state)
        self.assertEqual(list(out), ["hospitals"])
        self.assertEqual([h["name"] for h in out["hospitals"]], ["병원", "약국", "응급실"])  # 병원 → 약국 → 응급실
        self.hira.assert_called_once_with("정형외과", *CITY_HALL)  # Top-1 진료과로 검색
        self.drug.assert_called_once_with(*CITY_HALL)
        self.assertEqual(state, self.STATE)  # 입력 State는 건드리지 않는다

    def test_runs_searches_concurrently(self):
        def slow(*_):
            time.sleep(0.3)
            return []
        for m in (self.hira, self.drug, self.er):
            m.side_effect = slow
        t = time.time()
        search.search_hospitals(dict(self.STATE))
        self.assertLess(time.time() - t, 0.55)  # 순서대로면 0.9초 이상

    def test_no_location_no_search(self):
        for state in ({}, {"departments": self.STATE["departments"]},
                      {"departments": self.STATE["departments"], "location": {"lat": None, "lng": None}}):
            with self.subTest(state=state):
                self.assertEqual(search.search_hospitals(state), {"hospitals": []})
        for m in (self.hira, self.drug, self.er):
            m.assert_not_called()

    def test_without_department_only_pharmacy_and_er(self):
        out = search.search_hospitals({"departments": [], "location": {"lat": CITY_HALL[0], "lng": CITY_HALL[1]}})
        self.assertEqual([h["name"] for h in out["hospitals"]], ["약국", "응급실"])
        self.hira.assert_not_called()

    OK = {"status": "ok", "point": (37.45, 126.67), "name": "인천광역시 미추홀구", "candidates": []}
    AMBIGUOUS = {"status": "ambiguous", "point": None, "name": None, "candidates": [{"name": "서울특별시 중구"}]}

    def _region(self, result):
        p1 = mock.patch("backend.tools.hospital_db.available", return_value=True)
        p2 = mock.patch("backend.tools.hospital_db.resolve_region", return_value=result)
        p1.start()
        self.addCleanup(p1.stop)
        resolve = p2.start()
        self.addCleanup(p2.stop)
        return resolve

    def test_region_used_when_no_coordinates(self):
        resolve = self._region(self.OK)
        search.search_hospitals({**self.STATE, "location": {"region": "인천 미추홀구"}})
        resolve.assert_called_once_with("인천 미추홀구", None)
        self.hira.assert_called_once_with("정형외과", 37.45, 126.67)

    def test_region_overrides_gps(self):
        # "중구에서 안과 찾아줘"처럼 사용자가 말한 지역이 정해지면 GPS보다 우선
        resolve = self._region(self.OK)
        search.search_hospitals({**self.STATE, "location": {"lat": 35.1, "lng": 129.0, "region": "인천 미추홀구"}})
        resolve.assert_called_once_with("인천 미추홀구", (35.1, 129.0))  # 후보 정렬용으로 GPS도 넘김
        self.hira.assert_called_once_with("정형외과", 37.45, 126.67)

    def test_ambiguous_region_falls_back_to_gps_or_nothing(self):
        self._region(self.AMBIGUOUS)
        search.search_hospitals({**self.STATE, "location": {"lat": 35.1, "lng": 129.0, "region": "중구"}})
        self.hira.assert_called_once_with("정형외과", 35.1, 129.0)
        self.hira.reset_mock()
        self.assertEqual(search.search_hospitals({**self.STATE, "location": {"region": "중구"}}), {"hospitals": []})
        self.hira.assert_not_called()

    def test_find_region_without_db(self):
        with mock.patch("backend.tools.hospital_db.available", return_value=False):
            self.assertEqual(search.find_region("인천")["status"], "unavailable")
            self.assertEqual(search.search_hospitals({**self.STATE, "location": {"region": "인천"}}), {"hospitals": []})
        self.hira.assert_not_called()


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DepartmentCoverageTests(unittest.TestCase):
    """팀이 쓰는 진료과 이름은 모두 병원 검색이 되어야 한다.
    (분류는 data/departments/ 문서 이름으로 진료과를 고르고, 평가셋은 그 이름을 정답으로 쓴다)
    실패하면: 병원 검색이 모르는 진료과가 추가된 것 → hira.py의 DGSBJT/ALIASES에 추가 필요(최건)."""

    def test_department_docs_are_searchable(self):
        docs = sorted(f for f in os.listdir(os.path.join(REPO, "data", "departments")) if f.endswith(".md"))
        self.assertTrue(docs)
        for doc in docs:
            with self.subTest(doc=doc):
                self.assertIsNotNone(hira.resolve(doc[:-3])[1], f"{doc}: 병원 검색이 지원하지 않는 진료과")

    def test_testset_answers_are_searchable(self):
        with open(os.path.join(REPO, "eval", "testset.csv"), encoding="utf-8") as f:
            answers = {row["answer_dept"] for row in csv.DictReader(f)}
        self.assertIn("치과", answers)
        for dept in sorted(answers):
            with self.subTest(dept=dept):
                self.assertIsNotNone(hira.resolve(dept)[1], f"평가셋 정답 '{dept}': 병원 검색이 지원하지 않는 진료과")


@unittest.skipUnless(os.getenv("LIVE_API") == "1", "실제 API 호출은 LIVE_API=1 일 때만")
class LiveApiTests(unittest.TestCase):
    """실제 공공 API 호출 확인 (.env 키 필요). API 서버가 느리면 1~2분 걸린다.
    병원 DB가 있어도 API 경로를 검사하도록 DB는 끈다."""

    def setUp(self):
        clear_caches()
        pharmacy._cache.clear()
        p = mock.patch("backend.tools.hospital_db.available", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_pharmacy_live(self):
        out = pharmacy.search_pharmacies(*CITY_HALL)
        self.assertEqual(len(out), 3, "약국 API 결과 없음: 활용신청·키 확인")
        self.assertLess(out[0]["distance_m"], 1000)
        for p in out:
            HospitalOut(**p)

    def test_hira_live(self):
        out = hira.search_hospitals("정형외과", *CITY_HALL)
        self.assertEqual(len(out), 5, "심평원 API 결과 없음: .env 키·트래픽 한도·서버 상태 확인")
        self.assertTrue(all(h["distance_m"] < 3000 for h in out))
        for h in out:
            HospitalOut(**h)
        known = [h for h in out if h["is_open"] is not None]
        self.assertGreaterEqual(len(known), 3, f"진료시간을 찾은 병원이 너무 적음: {out}")

    def test_egen_live(self):
        out = egen.search_emergency(*CITY_HALL)
        self.assertEqual(len(out), 3, "E-Gen API 결과 없음: .env 키·트래픽 한도·서버 상태 확인")
        self.assertLess(out[0]["distance_m"], 5000)
        for h in out:
            HospitalOut(**h)

    def test_holidays_live(self):
        days = hours.holidays_of(2026)
        self.assertIsNotNone(days, "특일 정보 API 실패: 활용신청 확인")
        self.assertIn(date(2026, 12, 25), days)
        self.assertIn(date(2026, 10, 5), days)  # 대체공휴일(개천절)


if __name__ == "__main__":
    unittest.main()
