import time

import sentry_sdk
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from openai import APIError, APITimeoutError
from structlog.contextvars import bind_contextvars

from meety_ai.summary.schemas import SummaryRequest

logger = structlog.stdlib.get_logger(__name__)

summary_router = APIRouter()


@summary_router.post("/v1/summary")
async def generate_summary(payload: SummaryRequest, request: Request) -> Response:
    # 이 요청의 이후 로그를 회의 식별자로 조회할 수 있게 한다.
    bind_contextvars(meeting_id=payload.meeting_id)
    speaker_names = {
        speaker.speaker_id: speaker.display_name or f"화자 {speaker.speaker_id}"
        for speaker in payload.speakers
    }
    speaker_names[None] = "미확인 화자"
    transcript = "\n".join(
        f"{speaker_names.get(segment.speaker_id, f'화자 {segment.speaker_id}')}: {segment.content}"
        for segment in payload.segments
    )
    model = request.app.state.settings.summary_model
    started = time.perf_counter()
    try:
        markdown = await request.app.state.summary_chain.ainvoke(
            {
                "title": payload.title,
                "purpose": payload.purpose,
                "note": payload.note,
                "transcript": transcript,
                "previous_summary": payload.previous_summary,
                "regeneration_reason": payload.regeneration_reason,
            },
            config={
                "run_name": "meeting_summary",
                "metadata": {
                    "meeting_id": payload.meeting_id,
                    "request_id": payload.request_id,
                },
            },
        )
    except Exception as exc:
        # 예외 메시지에 프롬프트·응답 일부가 섞일 수 있어 타입과 공급자 상태코드만 남긴다.
        logger.warning(
            "summary_failed",
            model=model,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_type=type(exc).__name__,
            provider_status=getattr(exc, "status_code", None),
        )
        if isinstance(exc, APITimeoutError):
            sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=504, detail="LLM API 공급자 응답 시간이 초과되었습니다."
            ) from exc
        if isinstance(exc, APIError):
            sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=502, detail="LLM API 공급자 호출에 실패했습니다."
            ) from exc
        raise
    logger.info(
        "summary_completed",
        model=model,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return Response(markdown, media_type="text/markdown")
