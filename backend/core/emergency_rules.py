"""응급 1차 필터. 규칙 데이터: data/emergency_rules.json (FR-09).

False는 임상적인 응급 배제를 뜻하지 않는다. 부정문·과거력도 일치하면
True로 유지한다. 정책과 알려진 한계: docs/emergency_rules.md.
"""
import json
from pathlib import Path
import re
import unicodedata

RULES_PATH = Path(__file__).resolve().parents[2] / "data" / "emergency_rules.json"


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def _strings(value, field):
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and _normalize(item) for item in value
    ):
        raise ValueError(f"응급 규칙 {field}: 비어 있지 않은 문자열 목록이 필요합니다")
    return value


def load_rules(path=RULES_PATH):
    """잘못되거나 누락된 규칙은 시작 시 오류로 알린다. 빈 규칙으로 대체하지 않는다."""
    with Path(path).open(encoding="utf-8-sig") as source:
        data = json.load(source)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("지원하지 않는 응급 규칙 버전입니다")
    keywords = _strings(data.get("keywords"), "keywords")
    contexts = data.get("context_rules")
    if not isinstance(contexts, list):
        raise ValueError("context_rules 목록이 필요합니다")
    compiled = []
    for rule in contexts:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"]:
            raise ValueError("맥락 규칙 id가 필요합니다")
        words = tuple(_normalize(word) for word in _strings(rule.get("context_keywords"), "context_keywords"))
        raw_patterns = _strings(rule.get("patterns"), "patterns")
        if any(any(char.isspace() for char in pattern) for pattern in raw_patterns):
            raise ValueError("공백을 제거한 입력용 정규식에 공백 문자를 넣을 수 없습니다")
        patterns = tuple(re.compile(pattern) for pattern in raw_patterns)
        if any(pattern.search("") for pattern in patterns):
            raise ValueError("빈 문장에 일치하는 맥락 규칙은 허용하지 않습니다")
        compiled.append((words, patterns))
    return keywords, tuple(compiled)


# 기존 import 사용처와 호환되는 이름을 유지한다. 프로세스 시작 시 한 번 읽는다.
EMERGENCY_KEYWORDS, _CONTEXT_RULES = load_rules()
_NORMALIZED_KEYWORDS = tuple(_normalize(word) for word in EMERGENCY_KEYWORDS)


def is_emergency(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize(text)
    if any(word in normalized for word in _NORMALIZED_KEYWORDS):
        return True
    return any(
        any(word in normalized for word in words)
        and any(pattern.search(normalized) for pattern in patterns)
        for words, patterns in _CONTEXT_RULES
    )
