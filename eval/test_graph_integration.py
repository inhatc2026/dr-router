"""실제 LangGraph와 /api/chat 계약 검사. 외부 검색만 모의한다."""
import unittest
import tempfile
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.main import app
from backend.agent import graph
from backend.agent.nodes import search
from backend.tools import hospital_db
from eval.graph_fixture import create_database


ER = {"name": "테스트 응급실", "dept": "응급의학과", "kind": "응급실",
      "distance_m": 2100, "is_open": None, "phone": "032-000-0000",
      "address": "테스트 주소", "lat": 37.4, "lng": 126.6}
AMBIGUOUS = {"status": "ambiguous", "point": None,
             "candidates": [{"name": "인천 중구"}, {"name": "서울 중구"}]}


class GraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        graph._graph = None
        # 그래프 검사는 로컬 키·인덱스·모델 다운로드에 의존하지 않는다.
        # classify가 직접 import한 참조를 모의한다. 분류/RAG 자체 검사는 별도로 수행한다.
        rag_patch = patch("backend.agent.nodes.classify.retrieve", return_value=[])
        rag_patch.start()
        self.addCleanup(rag_patch.stop)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        graph._graph = None

    def post(self, message="이가 시려요", **fields):
        response = self.client.post("/api/chat", json={"message": message, **fields})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_emergency_bypasses_collect_and_classification_even_with_ambiguous_region(self):
        def must_not_collect(state):
            self.fail("응급 입력이 지역/증상 되묻기로 지연됨")
        with patch.object(graph.collect, "collect_symptoms", new=must_not_collect), \
             patch.object(search, "search_emergency_rooms", return_value={"hospitals": [ER]}) as er:
            data = self.post("피가 안 멈춰요", region="중구", history=[{"role": "assistant", "content": "언제부터인가요?"}])
        self.assertTrue(data["emergency"])
        self.assertFalse(data["need_more_info"])
        self.assertEqual(data["departments"], [])
        self.assertEqual(data["hospitals"], [ER])
        self.assertIn("119", data["reply"])
        self.assertEqual(er.call_args.args[0]["location"], {"region": "중구"})

    def test_ambiguous_region_requires_confirmation_with_or_without_gps(self):
        for gps in ({}, {"lat": 37.5, "lng": 126.6}):
            with self.subTest(gps=gps), patch.object(search, "find_region", return_value=AMBIGUOUS) as find, \
                 patch.object(search, "search_hospitals", side_effect=AssertionError("확인 전 검색")):
                graph._graph = None
                data = self.post(region="중구", **gps)
                self.assertTrue(data["need_more_info"])
                self.assertFalse(data["emergency"])
                self.assertIn("인천 중구", data["reply"])
                self.assertIn("지역 입력", data["reply"])
                self.assertEqual(data["hospitals"], [])
                self.assertEqual(data["departments"], [])
                find.assert_called_once_with("중구", near=(37.5, 126.6) if gps else None)

    def test_unknown_region_with_gps_does_not_silently_search_gps(self):
        with patch.object(search, "find_region", return_value={"status": "not_found"}), \
             patch.object(search, "search_hospitals", side_effect=AssertionError("확인 전 검색")):
            data = self.post(region="없는 지역", lat=37.5, lng=126.6)
        self.assertTrue(data["need_more_info"])
        self.assertEqual(data["hospitals"], [])

    def test_confirmed_region_and_history_reach_classify_and_search(self):
        seen = {}
        def classification_fixture(state):
            seen.update(state)
            return {"departments": [{"name": "치과", "score": .8, "reason": "테스트용 설명"}]}
        def search_fixture(state):
            self.assertEqual(state["departments"][0]["name"], "치과")
            return {"hospitals": [ER]}
        with patch.object(search, "find_region", return_value={"status": "ok", "point": (37.4, 126.6)}), \
             patch.object(graph.classify, "classify_department", new=classification_fixture), \
             patch.object(search, "search_hospitals", new=search_fixture):
            data = self.post(region="인천 중구", lat=37.5, lng=126.6,
                             history=[{"role": "user", "content": "찬물을 마시면 시려요"}])
        self.assertFalse(data["need_more_info"])
        self.assertEqual(data["departments"][0]["name"], "치과")
        self.assertIsNone(data["hospitals"][0]["is_open"])
        self.assertEqual(seen["location"], {"region": "인천 중구", "lat": 37.5, "lng": 126.6})
        self.assertEqual(seen["history"][0]["content"], "찬물을 마시면 시려요")

    def test_real_classifier_stub_preserves_empty_departments(self):
        with patch.object(search.pharmacy, "search_pharmacies", return_value=[]), \
             patch.object(search.egen, "search_emergency", return_value=[ER]), \
             patch.object(search.hira, "search_hospitals", side_effect=AssertionError("분류 없는 일반 병원 검색")):
            data = self.post(lat=37.5, lng=126.6)
        self.assertEqual(data["departments"], [])
        self.assertFalse(data["emergency"])
        self.assertEqual(data["hospitals"], [ER])

    def test_er_search_failure_keeps_emergency_advice(self):
        with patch.object(search, "search_emergency_rooms", side_effect=RuntimeError("lookup unavailable")):
            data = self.post("숨을 못 쉬어요", lat=37.5, lng=126.6)
        self.assertTrue(data["emergency"])
        self.assertIn("119", data["reply"])
        self.assertEqual(data["hospitals"], [])

    def test_slow_er_lookup_has_deadline_and_no_unbounded_queue(self):
        release, finished = Event(), Event()
        def slow_lookup(*args):
            try:
                release.wait(3)
                return []
            finally:
                finished.set()
        with patch.object(hospital_db, "available", return_value=False), \
             patch.object(search.egen, "_er_list", side_effect=slow_lookup) as lookup, \
             patch.object(graph, "ER_LOOKUP_BUDGET_SECONDS", .05):
            try:
                start = monotonic()
                data = self.post("숨을 못 쉬어요", lat=37.47, lng=126.62)
                self.assertLess(monotonic() - start, .8)
                self.assertFalse(finished.is_set())
                self.assertTrue(data["emergency"])
                self.assertEqual(data["hospitals"], [])
                self.post("피가 안 멈춰요", lat=37.47, lng=126.62)
                self.post("가슴이 아파요", lat=37.47, lng=126.62)
                self.assertEqual(lookup.call_count, 2)
            finally:
                release.set()
                # 모든 작업이 종료되고 슬롯이 반납된 뒤 다음 검사를 실행한다.
                graph._er_executor.submit(lambda: None).result(timeout=3)
                graph._er_executor.submit(lambda: None).result(timeout=3)

    def test_missing_region_db_never_silently_uses_gps(self):
        for gps in ({}, {"lat": 37.47, "lng": 126.62}):
            with self.subTest(gps=gps), tempfile.TemporaryDirectory() as directory, \
                 patch.object(hospital_db, "DB_PATH", str(Path(directory) / "absent.sqlite")), \
                 patch.object(search.egen, "search_emergency", side_effect=AssertionError("GPS로 대체됨")):
                data = self.post(region="부산 해운대구", **gps)
                self.assertTrue(data["need_more_info"])
                self.assertIn("지역 검색 정보를 지금 사용할 수 없습니다", data["reply"])
                self.assertEqual(data["hospitals"], [])

    def test_real_region_db_chat_confirmation_preserves_symptoms_and_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fixture.sqlite")
            create_database(path)
            with patch.object(hospital_db, "DB_PATH", path):
                first = self.post("이가 시려요", region="중구", lat=37.47, lng=126.62)
                self.assertTrue(first["need_more_info"])
                history = [{"role": "user", "content": "이가 시려요"}, {"role": "assistant", "content": first["reply"]}]
                for answer in ("네 인천 중구 맞아요", "네"):
                    with self.subTest(answer=answer):
                        seen = []
                        def classification_fixture(state):
                            seen.extend(state["symptoms"])
                            return {"departments": []}
                        with patch.object(graph.classify, "classify_department", new=classification_fixture):
                            graph._graph = None
                            second = self.post(answer, region="중구", lat=37.47, lng=126.62, history=history)
                        self.assertFalse(second["need_more_info"])
                        self.assertIn("이가 시려요", seen)
                        self.assertNotIn(answer, seen)
                        self.assertIn("인천광역시 중구", second["reply"])
                        self.assertEqual({p["name"] for p in second["hospitals"]}, {"테스트 인천 약국", "테스트 인천 응급실"})
                        graph._graph = None
                        third = self.post("찬물에 더 시려요", region="중구", lat=37.47, lng=126.62,
                                          history=history + [{"role": "user", "content": answer}, {"role": "assistant", "content": second["reply"]}])
                        self.assertFalse(third["need_more_info"])
                rejected = self.post("아니요", region="중구", history=history)
                self.assertTrue(rejected["need_more_info"])


if __name__ == "__main__":
    unittest.main()
