"""공통 메시지 설정과 식별자·정수 검증 규칙을 정의한다."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel


class Message(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, extra="forbid", strict=True
    )


Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
]

NonNegativeInt = Annotated[int, Field(ge=0)]

MeetingId = NonNegativeInt
RecordingSessionId = NonNegativeInt
