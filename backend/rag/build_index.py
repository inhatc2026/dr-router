"""
RAG 인덱스 구축 (소유자: 고원영) — 최초 1회 + 문서가 바뀔 때마다 실행
data/departments/*.md (진료과 요약 문서) → 임베딩 → Chroma 적재

실행: python -m backend.rag.build_index
"""
import re
from pathlib import Path

from backend.rag.retriever import get_collection

DATA_DIR = Path("data/departments")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def main():
    docs = sorted(DATA_DIR.glob("*.md"))
    print(f"[build_index] 문서 {len(docs)}개 발견")
    if not docs:
        print("문서가 없습니다: data/departments/*.md 를 먼저 작성하세요.")
        return

    ids, documents, metadatas = [], [], []
    for p in docs:
        text = _COMMENT.sub("", p.read_text(encoding="utf-8")).strip()  # 양식 안내 주석 제거
        ids.append(p.stem)  # 파일명 = 진료과명 = id
        documents.append(text)
        metadatas.append({"dept": p.stem, "source": p.name})  # 출처(근거 문서명)

    col = get_collection()
    col.upsert(ids=ids, documents=documents, metadatas=metadatas)

    # 삭제된 문서가 인덱스에 남지 않도록 정리
    stale = [i for i in col.get()["ids"] if i not in set(ids)]
    if stale:
        col.delete(ids=stale)
        print(f"[build_index] 삭제된 문서 제거: {stale}")

    print(f"적재 완료: {len(ids)}개 진료과 문서")


if __name__ == "__main__":
    main()
