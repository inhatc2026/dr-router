"""RAG 검색 (소유자: 고원영)

진료과 요약 문서(data/departments/*.md)를 Chroma에서 검색한다.
컬렉션 생성은 이 모듈 한 곳에서만 하고, build_index.py 가 가져다 쓴다.
"""
from typing import Dict, List

from backend.config import CHROMA_DIR

COLLECTION = "departments"
# 한국어 경량 임베딩 (기본). 품질을 더 원하면 "BAAI/bge-m3" (더 무거움)
MODEL = "jhgan/ko-sroberta-multitask"

_col = None


def get_collection():
    """Chroma 컬렉션을 1회 생성해 재사용한다."""
    global _col
    if _col is None:
        import chromadb
        from chromadb.utils import embedding_functions

        client = chromadb.PersistentClient(path=CHROMA_DIR)
        emb = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=MODEL)
        _col = client.get_or_create_collection(
            name=COLLECTION,
            embedding_function=emb,
            metadata={"hnsw:space": "cosine"},  # 코사인 유사도
        )
    return _col


def retrieve(query: str, k: int = 3) -> List[Dict]:
    """증상 쿼리로 관련 진료과 문서 Top-k를 검색한다.

    반환: [{"dept": 진료과명, "text": 문서 본문, "score": 0~1 (높을수록 유사)}, ...]
    인덱스가 없거나 오류가 나면 빈 리스트를 반환한다 (데모가 멈추지 않게).
    """
    if not query or not query.strip():
        return []
    try:
        col = get_collection()
        n = col.count()
        if n == 0:
            print("[retriever] 인덱스가 비었습니다. python -m backend.rag.build_index 를 먼저 실행하세요.")
            return []
        res = col.query(query_texts=[query], n_results=min(k, n))
    except Exception as e:  # noqa: BLE001
        print(f"[retriever] 검색 실패: {e}")
        return []

    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]
    return [
        {"dept": m.get("dept"), "text": d, "score": round(1 - dist, 3)}
        for d, m, dist in zip(docs, metas, dists)
    ]
