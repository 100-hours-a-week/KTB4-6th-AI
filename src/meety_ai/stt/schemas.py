"""공급자와 관계없이 전달하는 확정 전사 구간을 정의한다."""

from pydantic import BaseModel

from meety_ai.core.schemas import NonNegativeInt


class STTTranscript(BaseModel):
    """현재 STT 연결에 전송한 누적 오디오 기준의 시간 정보를 담는다."""

    content: str
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    # 회의 내 화자 식별자이며, 참가자 ID와는 별개다. 식별하지 못하면 null이다.
    speaker_id: NonNegativeInt | None = None
