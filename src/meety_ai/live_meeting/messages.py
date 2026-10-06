"""회의 text 메시지의 형식과 크기를 검증한다."""

import json

from pydantic import TypeAdapter, ValidationError

from meety_ai.chat.schemas import ChatFailed, ChatFailedPayload, ChatRequest, Identifier
from meety_ai.recording.schemas import ClientEvent, client_event_adapter
from meety_ai.recording.session import SessionProtocolError

MAX_TEXT_SIZE = 2 * 1024 * 1024
MAX_CONTROL_TEXT_SIZE = 4 * 1024
MAX_CHAT_TEXT_SIZE = 256 * 1024
identifier_adapter = TypeAdapter(Identifier)


def parse_meeting_message(text: str) -> ChatRequest | ClientEvent | ChatFailed:
    """검증된 요청을 반환한다. 식별 가능한 질문 오류는 연결을 유지할 응답으로 반환한다."""
    text_size = len(text.encode("utf-8"))
    if text_size > MAX_TEXT_SIZE:
        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise SessionProtocolError("invalid_message", "메시지 형식이 올바르지 않습니다.") from error

    if not isinstance(raw, dict) or raw.get("type") != "chat.request":
        if text_size > MAX_CONTROL_TEXT_SIZE:
            raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")
        return client_event_adapter.validate_python(raw)

    # 두 식별자까지 읽을 수 없으면 연결 오류 규칙을 적용한다.
    ids = {
        "meeting_id": identifier_adapter.validate_python(raw.get("meetingId")),
        "ai_request_id": identifier_adapter.validate_python(raw.get("aiRequestId")),
    }
    if text_size > MAX_CHAT_TEXT_SIZE:
        message = "질문 메시지가 너무 큽니다."
    else:
        try:
            return ChatRequest.model_validate(raw)
        except ValidationError:
            message = "질문 형식이 올바르지 않습니다."
    return ChatFailed(
        type="chat.failed",
        **ids,
        payload=ChatFailedPayload(code="invalid_request", message=message),
    )
