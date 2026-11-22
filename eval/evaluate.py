"""
Top-3 정확도 평가 (소유자: 박태준)
testset.csv(증상→정답 진료과)에 대해 에이전트의 추천 상위 3개 안에
정답이 들어있는 비율을 측정한다.

실행: python -m eval.evaluate --mode classify (분류만, 기본)
      python -m eval.evaluate --mode pipeline (응급 분기를 포함한 전체 흐름)
"""
import argparse
import csv
from pathlib import Path

from backend.agent.graph import run_agent
from backend.api.schemas import ChatRequest
from backend.agent.nodes.classify import classify_department
from backend import config

TESTSET = Path(__file__).with_name("testset.csv")


def top_k_departments(message: str, k: int = 3, mode: str = "classify"):
    if mode == "classify":
        result = classify_department({"user_input": message})
        return [d["name"] for d in result["departments"][:k]]
    if mode == "pipeline":
        res = run_agent(ChatRequest(message=message))
        return [d.name for d in res.departments[:k]]
    raise ValueError(f"Unknown evaluation mode: {mode}")


def main():
    parser = argparse.ArgumentParser(description="실제 RAG/LLM을 사용하는 유료 분류 평가")
    parser.add_argument("--mode", choices=("classify", "pipeline"), default="classify")
    args = parser.parse_args()
    if not config.LLM_API_KEY:
        parser.error("LLM_API_KEY 설정이 필요합니다. 키 없이 나온 빈 결과를 정확도로 평가하지 않습니다.")
    with TESTSET.open(encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    print(f"평가 모드: {args.mode} · 모델: {config.LLM_MODEL}")
    if args.mode == "pipeline":
        print("응급 분기로 추천이 생략되는 문항도 오답에 포함됩니다. 순수 분류 정확도와 구분하세요.")
    hit = 0
    for r in rows:
        preds = top_k_departments(r["symptom"], k=3, mode=args.mode)
        ok = r["answer_dept"] in preds
        hit += int(ok)
        print(f"[{'O' if ok else 'X'}] {r['symptom'][:20]}... → {preds} (정답: {r['answer_dept']})")

    total = len(rows)
    acc = hit / total if total else 0
    print(f"\nTop-3 정확도: {hit}/{total} = {acc:.1%}")
    if total < 20:
        print("주의: 현재 평가셋은 20건 미만이므로 요구사항의 평가 문항 수를 충족하지 않습니다.")


if __name__ == "__main__":
    main()
