"""
API 요청/응답 모델 — ★ 프론트↔백엔드 공유 계약 (소유자: 최건)
프론트의 fetch 본문/응답이 이 모델과 일치해야 한다.
"""
from pydantic import BaseModel, Field, model_serializer
from typing import List, Optional


class ChatRequest(BaseModel):
    message: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    history: List[dict] = []
    region: Optional[str] = None            # 위치 거부 시 지역명 (예: "인천 미추홀구") → State location["region"]


class DepartmentOut(BaseModel):
    name: str
    score: float
    reason: str
    sources: List[str] = Field(default_factory=list)  # FR-13: RAG 근거 문서명 (추가 선택 필드)


class HospitalOut(BaseModel):
    name: str
    dept: str                               # 진료과 (약국은 "약국", 응급실은 "응급의학과")
    distance_m: int
    is_open: Optional[bool] = None          # True 진료중 / False 휴진·진료시간 밖 / None 정보 없음
    phone: Optional[str] = None
    kind: str = "병원"                       # "병원" / "약국" / "응급실" (목록 필터용)
    address: Optional[str] = None
    lat: Optional[float] = None             # 지도 표시용
    lng: Optional[float] = None
    er_beds: Optional[int] = None           # 응급실만: 실시간 가용병상(0 이하 = 포화), 모르면 응답에서 빠짐 (추가 선택 필드)
    er_beds_at: Optional[str] = None        # 응급실만: 기관이 병상을 입력한 시각 "YYYY-MM-DDTHH:MM" (한국 시간)

    @model_serializer(mode="wrap")
    def _omit_unknown_beds(self, handler):
        """병상을 모르면 두 칸을 빼서, 병원·약국과 병상 정보 없는 응급실의 응답을 이전과 똑같이 유지한다."""
        data = handler(self)
        if isinstance(data, dict):
            for key in ("er_beds", "er_beds_at"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


class ChatResponse(BaseModel):
    reply: str                              # 사용자에게 보여줄 메시지
    emergency: bool = False                 # 응급이면 True → 프론트에서 119 안내
    need_more_info: bool = False            # True면 되묻는 중
    departments: List[DepartmentOut] = []   # 추천 진료과 (Top-N)
    hospitals: List[HospitalOut] = []       # 주변 의료기관
