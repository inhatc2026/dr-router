"""API 엔드포인트 (소유자: 최건)"""
from fastapi import APIRouter

from backend.api.schemas import ChatRequest, ChatResponse
from backend.agent.graph import run_agent
from backend.tools import egen

# 서버가 뜰 때만 응급실 실시간 병상 조회를 켠다 (서버 없이 도는 테스트는 병상 API를 부르지 않음)
router = APIRouter(on_startup=[egen.enable_realtime_beds], on_shutdown=[egen.disable_realtime_beds])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """증상 입력 → 에이전트 실행 → 진료과/병원 안내 반환"""
    return run_agent(req)
