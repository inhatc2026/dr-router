"""이번 주 응급 규칙 회귀 검사. API 키나 외부 패키지 없이 실행한다."""
import unittest
import unicodedata
import json
from pathlib import Path
import re
import tempfile

from backend.agent.nodes.triage import check_emergency
from backend.core.emergency_rules import is_emergency, load_rules, RULES_PATH


class EmergencyRulesTests(unittest.TestCase):
    def test_rule_file_rejects_missing_corrupt_or_empty_rules(self):
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            with self.assertRaises(FileNotFoundError):
                load_rules(path)
            invalid = ["{broken", json.dumps({**data, "version": 2}),
                       json.dumps({**data, "keywords": []}),
                       json.dumps({**data, "keywords": [" \t"]}),
                       json.dumps({**data, "context_rules": [{}]})]
            for text in invalid:
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_rules(path)
            path.write_text(json.dumps(data), encoding="utf-8-sig")
            self.assertEqual(load_rules(path)[0], data["keywords"])
            for pattern in ["[", ".*", "목 부었", "침\t못삼"]:
                modified = {**data, "context_rules": [{"id": "bad", "context_keywords": ["치과"], "patterns": [pattern]}]}
                path.write_text(json.dumps(modified), encoding="utf-8")
                with self.subTest(pattern=pattern), self.assertRaises((ValueError, re.error)):
                    load_rules(path)

    def test_danger_signs_in_user_language(self):
        cases = [
            "갑자기 가슴이 쥐어짜듯 아파요",  # 가이드의 필수 예시
            "아버지가 쓰러졌어요",
            "방금 정신을 잃었어요",
            "불러도 반응이 없어요",
            "깨워도 안 일어나요",
            "아이가 경련을 해요",
            "왼쪽 팔에 힘이 안 들어가요",
            "말이 어눌해졌어요",
            "오른쪽 입꼬리가 처졌어요",
            "갑자기 보이지 않아요",
            "호흡하기 어려워요",
            "호흡이 힘들어요",
            "숨을 쉬기 힘들어요",
            "입술이 파랗게 변했어요",
            "식은땀이 나고 어지러워요",
            "상처에서 피가 멈추지 않아요",
            "코에서 피가 안 멈춰요",
            "피를 토했어요",
            "배가 견딜 수 없을 만큼 아파요",
            "극심한 두통이 있어요",
            "음식을 먹고 혀가 붓고 있어요",
            "벌에 쏘이고 목구멍이 부었어요",
            "약을 과다 복용했어요",
            "세제를 마셨어요",
            "전신 화상을 입었어요",
            "전기에 감전됐어요",
            "상처 사이로 뼈가 보여요",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))

    def test_colloquial_variants_and_inflections(self):
        # 1차 목록이 놓치던 동의어·어형 변화·조사 생략·부사 삽입 표현.
        cases = [
            # 의식 및 신경
            "기절했어요", "할머니가 갑자기 졸도하셨어요", "정신 잃었어요",
            "아기가 열경기를 해요", "아이가 경기를 일으켰어요",
            "팔에 힘이 없어요", "다리에 힘이 빠져요", "팔에 감각이 없어요",
            "갑자기 눈이 안 보여요", "갑자기 앞이 안 보여요",
            "말이 잘 안 나와요", "혀가 꼬여서 말이 이상해요", "입이 비뚤어졌어요",
            # 호흡 및 기도
            "호흡이 곤란해요", "호흡이 가빠요", "떡이 목에 걸렸어요",
            "입술이 새파래졌어요", "입술이 보라색이에요",
            # 흉부 및 순환
            "심장이 아파요", "심장이 조여 와요", "흉부 통증이 있어요",
            "심장이 멈춘 것 같아요", "맥박이 없어요",
            # 출혈
            "피가 너무 많이 나요", "피 많이 나요", "피가 철철 나요",
            "피가 안 멎어요", "피가 멎지 않아요", "지혈이 되지 않아요",
            "출혈이 심해요", "피 토했어요",
            # 극심한 통증
            "참을 수가 없어요", "견딜 수가 없어요", "죽을 것 같이 아파요",
            # 알레르기 부종: '부어'와 같은 뜻의 과거형·관형형
            "입술이 부었어요", "새우를 먹고 목이 부었어요", "입술이 부은 것 같아요",
            # 중독 및 중증 외상
            "약을 많이 먹었어요", "수면제를 한꺼번에 먹었어요", "농약 마셨어요",
            "락스를 마셨어요", "연탄가스를 마신 것 같아요",
            "화상을 심하게 입었어요", "넘어졌는데 뼈가 튀어나왔어요",
            "해열제 한 통을 다 먹었어요", "감기약 한 통 먹었어요",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))

    def test_context_limited_expressions_skip_everyday_sentences(self):
        # 운동·음식·일상 대화에서도 흔한 표현은 증상·약물 맥락이 붙을 때만 검출한다.
        # (위험 문장, 같은 단어가 들어가거나 공백 제거 후 글자가 겹치는 문장)
        pairs = [
            ("감기약 한 통을 다 먹었어요", "약한 통증이 있어요"),
            ("아기가 경기를 했어요", "축구 경기를 했는데 무릎이 아파요"),
            ("아이가 경기를 해요", "아이가 축구 경기를 하고 와서 다리가 뻐근해요"),
            ("약을 한 통 다 먹었어요", "아이스크림 한 통을 다 먹었더니 배가 아파요"),
            ("수면제 한 통을 먹었어요", "과자 한 통을 다 먹었어요"),
            ("죽을 만큼 아파요", "죽을 만큼 아이스크림이 먹고 싶어요"),
            ("너무 아파서 못 참겠어요", "배고파서 못 참겠어요"),
            ("피를 너무 많이 흘렸어요", "커피를 너무 많이 마셨어요"),
            ("기침할 때 피가 나와요", "기침할 때 피곤하고 목이 아파요"),
            ("얼굴에 감각이 없어요", "방향 감각이 없어서 길을 자주 헤매요"),
            ("불러도 대답이 없어요", "문자를 보냈는데 대답이 없어요"),
            ("다리가 부러져서 뼈가 튀어나왔어요", "광대뼈가 튀어나와서 고민이에요"),
        ]
        for danger, everyday in pairs:
            with self.subTest(text=danger):
                self.assertTrue(is_emergency(danger))
            with self.subTest(text=everyday):
                self.assertFalse(is_emergency(everyday))
        # 증상 맥락으로 좁힐 형태가 없어 제외한 표현
        self.assertFalse(is_emergency("일이 많아서 정신을 못 차리겠어요"))

    def test_guide_part1_keywords_still_match(self):
        # 가이드 PART 1 STEP 1 목록을 모두 계속 검출하는지 확인한다.
        guide_keywords = [
            "의식", "의식저하", "의식 저하", "쓰러", "실신", "경련", "발작",
            "마비", "한쪽", "말이 어눌", "발음이", "얼굴이 돌아",
            "숨", "호흡곤란", "숨을 못", "숨이 안", "숨이 차",
            "가슴", "흉통", "가슴이 찢", "가슴을 쥐어", "식은땀",
            "피를 많이", "대량 출혈", "토혈", "각혈", "멈추지 않는 피",
            "극심한", "참을 수 없는", "목을 조",
        ]
        for kw in guide_keywords:
            with self.subTest(keyword=kw):
                self.assertTrue(is_emergency(kw))
        # 가이드 목록의 '갑자기'는 현행 정책상 단독으로 검출하지 않는다(월요일 논의).
        self.assertFalse(is_emergency("갑자기"))

    def test_inputs_without_matching_signals(self):
        # False는 이 문장에 등록 표현이 없다는 뜻이며 임상 판정이 아니다.
        for text in ["무릎이 시큰해요", "콧물과 재채기가 나요",
                     "피부가 가려워요", "이가 시려요", "병원 위치를 알려주세요",
                     "", " \t\n"]:
            with self.subTest(text=text):
                self.assertFalse(is_emergency(text))

    def test_spacing_and_unicode_variants(self):
        for text in ["피 가 안 멈 춰 요", "호흡\t곤란", "말이\n어눌해요",
                     "과다\u3000복용", "참을수없는통증",
                     unicodedata.normalize("NFD", "반응이 없어요")]:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))

    def test_negation_and_history_remain_conservative(self):
        for text in ["흉통은 없어요", "어제 실신했지만 지금 괜찮아요",
                     "열은 없지만 호흡이 어려워요",
                     "호흡곤란은 없지만 피가 안 멈춰요",
                     "한쪽 무릎이 시큰해요"]:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))

    def test_triage_returns_emergency_boolean(self):
        # 노드는 변경 필드만 반환할 수 있으므로 전체 State 반환을 요구하지 않는다.
        for text, expected in [("피가 안 멈춰요", True), ("무릎이 시큰해요", False)]:
            with self.subTest(text=text):
                state = {"user_input": text, "symptoms": [text],
                         "location": {"lat": 37.5, "lng": 126.6},
                         "emergency": not expected}
                result = check_emergency(state)
                self.assertIs(result["emergency"], expected)

    def test_triage_with_missing_input(self):
        self.assertIs(check_emergency({})["emergency"], False)

    def test_dental_symptoms_with_neck_swelling_or_dysphagia(self):
        cases = [
            "사랑니 쪽이 붓더니 얼굴이랑 목까지 부었고 열이 나요",
            "잇몸이 붓고 침 삼키기가 힘들어요",
            "치통이 있는데 목까지 퉁퉁 부었어요",
            "어금니가 아프고 목 주변까지 붓고 있어요",
            "치아가 아프고 목도 부었어요",
            "발치 후 음식 삼키기가 너무 어려워요",
            "이를 뽑고 침을 못 삼켜요",
            "잇몸이 붓고 삼킬 수가 없어요",
            "사랑니가 아프고 삼키는 게 힘들어요",
            "잇몸이 붓고 침을 삼키지 못해요",
            "치통이 있고 삼키기 어렵습니다",
            "이가 아프고 목까지 부었어요",
            "충치가 있는데 목까지 부었어요",
            "이 뽑았는데 침을 못 삼켜요",
            "잇몸이 붓고 침 삼키기가 좀 힘들어요",
            "사랑니 때문에 밥을 못 삼켜요",
            "잇몸이 부어서 아무것도 못 삼키겠어요",
            "치통이 있고 목 부었어요",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))

    def test_dental_evaluation_questions_remain_unmatched(self):
        # PR #3의 분류 평가 문항. False는 임상적 비응급 판정이 아니다.
        cases = [
            "어금니가 욱신거리고 찬물을 마시면 이가 시려요",
            "양치할 때 잇몸에서 피가 나고 잇몸이 부었어요",
            "사랑니 쪽 잇몸이 붓고 음식 씹을 때 아파요",
            "이가 깨졌는데 뜨거운 걸 먹으면 찌릿해요",
            "치과 예약을 하고 목도리를 샀어요",
            "치통 때문에 음식을 씹기가 힘들어요",
            "사랑니가 아프고 침 삼킬 때 따끔해요",
            "이가 아프고 목도리를 샀어요",
            "충치가 있어서 치과를 예약했어요",
            "이 뽑기 게임을 했어요",
            "잇몸이 붓고 밥 먹기가 불편해요",
            "사랑니 때문에 밥을 천천히 먹어요",
            "이가 아프고 손목 부었어요",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(is_emergency(text))

    def test_dental_rules_keep_normalization_and_conservative_policy(self):
        for text in ["잇 몸 이 붓 고 침\n삼키기가\t힘들어요",
                     unicodedata.normalize("NFD", "사랑니가 아프고 목까지 부었어요"),
                     "잇몸이 붓고 침 삼키기가 힘들지는 않아요",
                     "이를 뽑았는데 피가 안 멈춰요"]:
            with self.subTest(text=text):
                self.assertTrue(is_emergency(text))


if __name__ == "__main__":
    unittest.main()
