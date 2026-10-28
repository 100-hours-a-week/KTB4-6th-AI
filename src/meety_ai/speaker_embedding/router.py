import asyncio
import time

import modal
import sentry_sdk
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from meety_ai.speaker_embedding.schemas import SpeakerEmbeddingRequest, SpeakerEmbeddingResponse

logger = structlog.stdlib.get_logger(__name__)

speaker_embedding_router = APIRouter()

# Modal 함수가 반환한 처리 실패 코드별 응답 상태.
_MODAL_ERROR_STATUS = {"download_failed": 502, "invalid_audio": 422, "inference_failed": 502}


def _error(status_code: int, error_code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"errorCode": error_code})


async def embed_speaker(audio_url: str, timeout_seconds: float) -> SpeakerEmbeddingResponse:
    """배포된 Modal 함수를 호출해 사전 녹음의 등록 벡터를 반환한다.

    예외 메시지에 URL이 섞일 수 있어 로그에는 예외 타입만 남기고, 벡터는 남기지 않는다.
    """
    started = time.perf_counter()
    try:
        # 함수 조회도 SDK 호출이므로 인증·미배포 오류를 같은 경계에서 처리한다.
        function = modal.Function.from_name("meety-speaker-embedding", "embed_speaker")
        async with asyncio.timeout(timeout_seconds):
            result = await function.remote.aio(audio_url)
        duration_ms = round((time.perf_counter() - started) * 1000)
    except (TimeoutError, modal.exception.TimeoutError) as exc:
        sentry_sdk.capture_exception(exc)
        logger.warning(
            "speaker_embedding_timeout",
            error_type=type(exc).__name__,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        raise _error(504, "timeout") from None
    except modal.exception.Error as exc:
        sentry_sdk.capture_exception(exc)
        logger.warning(
            "speaker_embedding_call_failed",
            error_type=type(exc).__name__,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        raise _error(502, "inference_failed") from None

    if isinstance(result, dict) and result.get("status") == "error":
        error_code = result.get("error_code")
        if error_code not in _MODAL_ERROR_STATUS:
            error_code = "inference_failed"
        sentry_sdk.capture_exception(
            RuntimeError("화자 임베딩 처리 실패"), tags={"error_code": error_code}
        )
        logger.warning("speaker_embedding_failed", error_code=error_code, duration_ms=duration_ms)
        raise _error(_MODAL_ERROR_STATUS[error_code], error_code)
    try:
        if result["status"] != "ok":
            raise ValueError("알 수 없는 status")
        response = SpeakerEmbeddingResponse(
            model_version=result["model_version"], embedding=result["embedding"]
        )
    except (TypeError, KeyError, ValueError) as exc:
        # ValidationError는 ValueError의 하위 타입이다.
        # 오류 메시지에 벡터 원문이 섞이므로 Sentry에도 예외 타입만 보낸다.
        sentry_sdk.capture_exception(RuntimeError(f"화자 임베딩 응답 오류: {type(exc).__name__}"))
        logger.warning("speaker_embedding_invalid_response", error_type=type(exc).__name__)
        raise _error(502, "inference_failed") from None
    logger.info(
        "speaker_embedding_completed",
        duration_ms=duration_ms,
        model_version=response.model_version,
        dimensions=len(response.embedding),
    )
    return response


@speaker_embedding_router.post("/v1/speaker-embedding")
async def generate_speaker_embedding(request: Request) -> SpeakerEmbeddingResponse:
    # 기본 422 응답은 입력 원문을 포함하므로 presigned URL이 노출되지 않도록 직접 검증한다.
    try:
        payload = SpeakerEmbeddingRequest.model_validate_json(await request.body())
    except ValidationError:
        raise _error(422, "invalid_request") from None
    return await embed_speaker(
        payload.audio_url, request.app.state.settings.speaker_embedding_timeout_seconds
    )
