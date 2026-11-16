"""병원·응급실 검색(최건 담당) 테스트.

기본 테스트는 실제 API를 부르지 않으므로 API 키나 인터넷 없이 실행된다.
    python -X utf8 -m unittest eval.test_hospital_search -v

실제 공공 API까지 확인하려면 (.env에 키 필요, 1~2분 걸림):
    PowerShell:  $env:LIVE_API="1"; python -X utf8 -m unittest eval.test_hospital_search -v
    Git Bash:    LIVE_API=1 python -X utf8 -m unittest eval.test_hospital_search -v
"""
import logging
import os
import unittest
from unittest import mock

import requests

from backend.agent.nodes import search
from backend.api.schemas import HospitalOut
from backend.tools import _common, egen, hira

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


def hira_item(name, distance, phone="02-000-0000", **extra):
    return {"yadmNm": name, "distance": str(distance), "telno": phone,
            "XPos": "126.97", "YPos": 37.56, **extra}


def er_item(name, lat, lng, tel1="02-111-1111", tel3="02-999-9999"):
    return {"dutyName": name, "wgs84Lat": lat, "wgs84Lon": lng, "dutyTel1": tel1, "dutyTel3": tel3}


def clear_caches():
    hira._cache.clear()
    egen._cache.update(at=0.0, items=[])


class FetchItemsTests(unittest.TestCase):
    """_common.fetch_items: 공공 API 응답을 항상 list로, 실패는 빈 list로."""

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
    def test_no_results(self, get):
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
            self.assertEqual(_common.fetch_items(self.URL, "", {}), [])
        get.assert_not_called()

    @mock.patch("backend.tools._common.requests.get")
    def test_error_result_code(self, get):
        get.return_value = api_response(None, code="30", msg="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
        with self.assertLogs("backend.tools._common", logging.WARNING) as logs:
            self.assertEqual(_common.fetch_items(self.URL, "KEY", {}), [])
        self.assertIn("30", "".join(logs.output))

    @mock.patch("backend.tools._common.requests.get")
    def test_network_failures_return_empty_without_leaking_key(self, get):
        secret = "SECRET-KEY-123=="
        for exc in (requests.Timeout(f"timeout ?serviceKey={secret}"),
                    requests.ConnectionError(f"refused ?serviceKey={secret}"),
                    requests.HTTPError(f"403 ?serviceKey={secret}")):
            with self.subTest(exc=type(exc).__name__):
                get.side_effect = exc
                with self.assertLogs("backend.tools._common", logging.WARNING) as logs:
                    self.assertEqual(_common.fetch_items(self.URL, secret, {}), [])
                self.assertNotIn(secret, "".join(logs.output))

    @mock.patch("backend.tools._common.requests.get")
    def test_non_json_response(self, get):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")  # 예: XML 오류 페이지
        get.return_value = resp
        with self.assertLogs("backend.tools._common", logging.WARNING):
            self.assertEqual(_common.fetch_items(self.URL, "KEY", {}), [])


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
        patcher = mock.patch("backend.tools.hira.fetch_items")
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def test_sorted_by_distance_and_limited(self):
        # API 결과는 거리순이 아니다(실측) → 직접 정렬해야 한다
        self.fetch.return_value = [hira_item(f"병원{d}", d) for d in (900, 120, 450, 300, 700, 50)]
        out = hira.search_hospitals("정형외과", *CITY_HALL, top=5)
        self.assertEqual([h["distance_m"] for h in out], [50, 120, 300, 450, 700])

    def test_matches_api_contract(self):
        self.fetch.return_value = [hira_item("튼튼정형외과의원", "320.7", phone="02-123-4567")]
        out = hira.search_hospitals("정형외과", *CITY_HALL)
        self.assertEqual(out, [{"name": "튼튼정형외과의원", "dept": "정형외과", "distance_m": 320,
                                "is_open": True, "phone": "02-123-4567"}])
        HospitalOut(**out[0])  # schemas.py 계약 위반이면 예외

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
        with self.assertLogs("backend.tools.hira", logging.WARNING):
            self.assertEqual(hira.search_hospitals("치과", *CITY_HALL), [])
        self.fetch.assert_not_called()

    def test_missing_location(self):
        self.assertEqual(hira.search_hospitals("내과", None, None), [])
        self.fetch.assert_not_called()

    def test_widens_radius_when_too_few(self):
        self.fetch.side_effect = [[hira_item("가까운곳", 800)],
                                  [hira_item("가까운곳", 800), hira_item("먼곳", 15000)]]
        out = hira.search_hospitals("안과", *CITY_HALL, top=2)
        radii = [c.args[2]["radius"] for c in self.fetch.call_args_list]
        self.assertEqual(radii, list(hira.RADII))
        self.assertEqual([h["name"] for h in out], ["가까운곳", "먼곳"])

    def test_stops_at_first_radius_when_enough(self):
        self.fetch.return_value = [hira_item(f"병원{i}", i * 100) for i in range(1, 6)]
        hira.search_hospitals("내과", *CITY_HALL, top=5)
        self.assertEqual(self.fetch.call_count, 1)

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

    def test_cache_hit_skips_api(self):
        self.fetch.return_value = [hira_item(f"병원{i}", i) for i in range(5)]
        # 캐시는 좌표를 소수 3자리(약 100m)로 묶는다. 반올림 경계를 피한 두 점(약 10m 차이)
        first = hira.search_hospitals("내과", 37.5661, 126.9781)
        second = hira.search_hospitals("내과", 37.5662, 126.9782)
        self.assertEqual(first, second)
        self.assertEqual(self.fetch.call_count, 1)
        hira.search_hospitals("외과", 37.5661, 126.9781)  # 진료과가 다르면 새로 조회
        self.assertEqual(self.fetch.call_count, 2)

    def test_failure_is_not_cached(self):
        self.fetch.return_value = []  # API 실패 → 빈 결과
        self.assertEqual(hira.search_hospitals("내과", *CITY_HALL), [])
        self.fetch.return_value = [hira_item("A", 10)] * 5
        self.assertEqual(len(hira.search_hospitals("내과", *CITY_HALL)), 5)


class EgenSearchTests(unittest.TestCase):
    """egen.search_emergency: 좌표 → 가까운 응급실 목록."""

    def setUp(self):
        clear_caches()
        patcher = mock.patch("backend.tools.egen.fetch_items")
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

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
        self.fetch.return_value = []
        self.assertEqual(egen.search_emergency(*CITY_HALL), [])
        self.fetch.return_value = [er_item("A", 37.5670, 126.9785)]
        self.assertEqual(len(egen.search_emergency(*CITY_HALL)), 1)


class SearchNodeTests(unittest.TestCase):
    """④ search 노드: State 규약대로 hospitals 칸만 돌려준다."""

    STATE = {"user_input": "무릎이 아파요",
             "departments": [{"name": "정형외과", "score": 0.9, "reason": "무릎 통증"},
                             {"name": "재활의학과", "score": 0.5, "reason": "보조"}],
             "location": {"lat": CITY_HALL[0], "lng": CITY_HALL[1]}}

    @mock.patch("backend.tools.egen.search_emergency")
    @mock.patch("backend.tools.hira.search_hospitals")
    def test_returns_only_hospitals_field(self, hira_search, er_search):
        hira_search.return_value = [{"name": "병원", "dept": "정형외과", "distance_m": 100, "is_open": True, "phone": None}]
        er_search.return_value = [{"name": "응급실", "dept": "응급의학과", "distance_m": 900, "is_open": True, "phone": None}]
        state = dict(self.STATE)
        out = search.search_hospitals(state)
        self.assertEqual(list(out), ["hospitals"])
        self.assertEqual([h["name"] for h in out["hospitals"]], ["병원", "응급실"])
        hira_search.assert_called_once_with("정형외과", *CITY_HALL)  # Top-1 진료과로 검색
        self.assertEqual(state, self.STATE)  # 입력 State는 건드리지 않는다

    @mock.patch("backend.tools.egen.search_emergency")
    @mock.patch("backend.tools.hira.search_hospitals")
    def test_skips_search_without_department_or_location(self, hira_search, er_search):
        for state in ({}, {"departments": []}, {"departments": self.STATE["departments"]},
                      {"departments": self.STATE["departments"], "location": {"lat": None, "lng": None}}):
            with self.subTest(state=state):
                self.assertEqual(search.search_hospitals(state), {"hospitals": []})
        hira_search.assert_not_called()
        er_search.assert_not_called()


@unittest.skipUnless(os.getenv("LIVE_API") == "1", "실제 API 호출은 LIVE_API=1 일 때만")
class LiveApiTests(unittest.TestCase):
    """실제 공공 API 호출 확인 (.env 키 필요). API 서버가 느리면 1~2분 걸린다."""

    def setUp(self):
        clear_caches()

    def test_hira_live(self):
        out = hira.search_hospitals("정형외과", *CITY_HALL)
        self.assertEqual(len(out), 5, "심평원 API 결과 없음: .env 키·트래픽 한도·서버 상태 확인")
        self.assertTrue(all(h["distance_m"] < 3000 for h in out))
        for h in out:
            HospitalOut(**h)

    def test_egen_live(self):
        out = egen.search_emergency(*CITY_HALL)
        self.assertEqual(len(out), 3, "E-Gen API 결과 없음: .env 키·트래픽 한도·서버 상태 확인")
        self.assertLess(out[0]["distance_m"], 5000)
        for h in out:
            HospitalOut(**h)


if __name__ == "__main__":
    unittest.main()
