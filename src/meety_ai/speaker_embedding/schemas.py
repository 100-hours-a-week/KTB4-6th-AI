from typing import Annotated

from pydantic import Field, FiniteFloat, StringConstraints

from meety_ai.core.schemas import Message


class SpeakerEmbeddingRequest(Message):
    # presigned URL은 서명을 포함하므로 로그·오류 응답에 원문을 남기지 않는다.
    # 호스트가 없는 URL은 다운로드 실패가 아니라 요청 형식 오류로 거절한다.
    audio_url: Annotated[str, StringConstraints(pattern=r"^https://[^/\s?#]+\S*$", max_length=4096)]


class SpeakerEmbeddingResponse(Message):
    model_version: Annotated[str, StringConstraints(min_length=1)]
    # NaN·Infinity·빈 배열은 성공 결과로 반환하지 않는다.
    embedding: Annotated[list[FiniteFloat], Field(min_length=1)]
