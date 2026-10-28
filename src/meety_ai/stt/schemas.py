"""공급자와 관계없이 전달하는 확정 전사 구간과 화자 연결 정보를 정의한다."""

import math
from typing import Annotated

from pydantic import BaseModel, Field, FiniteFloat, StringConstraints, model_validator

from meety_ai.core.schemas import Message, NonNegativeInt

EMBEDDING_DIM = 192


class STTTranscript(BaseModel):
    """현재 STT 연결에 전송한 누적 오디오 기준의 시간 정보를 담는다."""

    content: str
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    # 회의 내 화자 식별자이며, 참가자 ID와는 별개다. 식별하지 못하면 null이다.
    speaker_id: NonNegativeInt | None = None


class SpeakerEmbedding(Message):
    model_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    embedding: Annotated[
        list[FiniteFloat], Field(min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM)
    ]

    @model_validator(mode="after")
    def validate_norm(self):
        # 성분이 유한해도 제곱합이 넘치거나 0이 되면 정규화할 수 없다.
        squared = math.fsum(value * value for value in self.embedding)
        if not (math.isfinite(squared) and squared > 0):
            raise ValueError("정규화할 수 없는 등록 벡터입니다.")
        return self


class SpeakerProfile(Message):
    """STT 공급자에 전달하는 참가자 등록 벡터다. 이름은 포함하지 않는다."""

    team_member_id: NonNegativeInt
    speaker_embedding: SpeakerEmbedding


class SpeakerMapping(Message):
    speaker_id: NonNegativeInt
    # 화자는 구분했지만 참가자와 연결하지 않았으면 null이다.
    team_member_id: NonNegativeInt | None


class STTSpeakerMapping(BaseModel):
    """현재 STT 연결에서 발견한 화자 전체의 참가자 연결 스냅샷이다."""

    mappings: list[SpeakerMapping]
