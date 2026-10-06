"""회의 연결 예외를 안전한 오류 응답과 연결 종료 코드로 변환한다."""

import structlog
from pydantic import ValidationError

from meety_ai.recording.decoder import AudioDecodeError
from meety_ai.recording.schemas import SessionError, SessionErrorPayload
from meety_ai.recording.session import SessionProtocolError
from meety_ai.recording.stt_client import STTProviderError

logger = structlog.stdlib.get_logger(__name__)

MEETING_ERRORS = (
    ValidationError,
    SessionProtocolError,
    AudioDecodeError,
    TimeoutError,
    STTProviderError,
)


def build_error_response(error: Exception, request_id: str | None) -> tuple[SessionError, int]:
    """알려진 예외를 변환하고 입력 원문 없이 실패 정보를 기록한다."""
    retryable = False
    if isinstance(error, STTProviderError):
        code, message = "provider_unavailable", str(error)
        retryable = True
        request_id = None
    elif isinstance(error, ValidationError):
        code, message = "invalid_message", "메시지 형식이 올바르지 않습니다."
    elif isinstance(error, SessionProtocolError):
        code, message = error.code, str(error)
    elif isinstance(error, AudioDecodeError):
        code, message = "audio_decode_failed", "오디오 처리에 실패했습니다."
    elif isinstance(error, TimeoutError):
        code, message = "processing_timeout", "처리 대기 시간이 초과됐습니다."
    else:
        raise TypeError("변환할 수 없는 회의 연결 예외입니다.") from error

    close_code = 1008
    if code == "message_too_large":
        close_code = 1009
    elif code in ("audio_decode_failed", "processing_timeout", "provider_unavailable"):
        close_code = 1011

    log = logger.error if code == "provider_unavailable" else logger.warning
    # ValidationError 등의 메시지에는 입력 원문이 섞일 수 있어 예외 타입만 남긴다.
    log(
        "session_failed",
        code=code,
        close_code=close_code,
        request_id=request_id,
        error_type=type(error).__name__,
    )
    return (
        SessionError(
            type="error",
            request_id=request_id,
            payload=SessionErrorPayload(code=code, message=message, retryable=retryable),
        ),
        close_code,
    )
