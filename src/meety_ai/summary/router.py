import asyncio
import time

import sentry_sdk
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from langchain_core.runnables import Runnable
from openai import APIError, APITimeoutError
from structlog.contextvars import bind_contextvars

from meety_ai.core.errors import TaskError
from meety_ai.summary.schemas import SummaryRequest, SummarySegment

logger = structlog.stdlib.get_logger(__name__)

summary_router = APIRouter()
WORK_TIMEOUT_SECONDS = 180
_ERROR_STATUS = {"timeout": 504, "provider_failed": 502}


def speaker_label(segment: SummarySegment) -> str:
    if segment.display_name:
        return segment.display_name
    return "미확인 화자" if segment.speaker_id is None else f"화자 {segment.speaker_id}"


async def run_summary(payload: SummaryRequest, chain: Runnable, *, model: str) -> str:
    """회의 전사를 요약 Markdown으로 만든다. 실패는 TaskError로 전달한다."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(WORK_TIMEOUT_SECONDS):
            transcript = "\n".join(
                f"{speaker_label(segment)}: {segment.content}" for segment in payload.segments
            )
            markdown = await chain.ainvoke(
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
        if isinstance(exc, TimeoutError):
            raise TaskError("timeout", "요약 작업 제한 시간이 초과되었습니다.") from exc
        if isinstance(exc, APITimeoutError):
            sentry_sdk.capture_exception(exc)
            raise TaskError("timeout", "LLM API 공급자 응답 시간이 초과되었습니다.") from exc
        if isinstance(exc, APIError):
            sentry_sdk.capture_exception(exc)
            raise TaskError("provider_failed", "LLM API 공급자 호출에 실패했습니다.") from exc
        raise
    logger.info(
        "summary_completed",
        model=model,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return markdown


@summary_router.post("/v1/summary")
async def generate_summary(payload: SummaryRequest, request: Request) -> Response:
    # 이 요청의 이후 로그를 회의 식별자로 조회할 수 있게 한다.
    bind_contextvars(meeting_id=payload.meeting_id)
    try:
        markdown = await run_summary(
            payload,
            request.app.state.summary_chain,
            model=request.app.state.settings.summary_model,
        )
    except TaskError as error:
        raise HTTPException(_ERROR_STATUS[error.code], detail=str(error)) from error
    return Response(markdown, media_type="text/markdown")
