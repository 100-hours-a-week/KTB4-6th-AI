"""회의 질문을 처리하고 최종 답변 또는 실패를 HTTP로 반환한다."""

import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from structlog.contextvars import bind_contextvars

from meety_ai.chat.agent import ChatGenerationError, generate_answer
from meety_ai.chat.schemas import ChatRequest, ChatResponse

logger = structlog.stdlib.get_logger(__name__)
chat_router = APIRouter()


@chat_router.post("/v1/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def answer_question(payload: ChatRequest, request: Request) -> ChatResponse | JSONResponse:
    bind_contextvars(meeting_id=payload.meeting_id, ai_request_id=payload.ai_request_id)
    settings = request.app.state.settings
    if request.app.state.chat_model is None or settings.internal_api_key is None:
        return JSONResponse(
            status_code=503,
            content={
                "aiRequestId": payload.ai_request_id,
                "code": "generation_failed",
                "message": "챗봇 서버 설정이 준비되지 않았습니다.",
            },
        )
    started = time.perf_counter()
    try:
        answer = await generate_answer(
            payload,
            request.app.state.chat_model,
            request.app.state.backend_client,
            timeout_seconds=settings.chat_timeout_seconds,
        )
    except ChatGenerationError as error:
        status = {
            "invalid_request": 400,
            "forbidden": 403,
            "timeout": 504,
            "retrieval_failed": 502,
        }.get(error.code, 500)
        logger.warning(
            "chat_failed",
            code=error.code,
            error_message=str(error),
            error_type=type(error.__cause__ or error).__name__,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return JSONResponse(
            status_code=status,
            content={
                "aiRequestId": payload.ai_request_id,
                "code": error.code,
                "message": str(error),
            },
        )
    logger.info("chat_completed", duration_ms=round((time.perf_counter() - started) * 1000))
    return ChatResponse(ai_request_id=payload.ai_request_id, **answer.model_dump())
