"""전역 AI 채팅 답변을 SSE로 스트리밍한다."""

import time
from collections.abc import AsyncIterable

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from structlog.contextvars import bind_contextvars

from meety_ai.assistant.agent import stream_answer
from meety_ai.assistant.schemas import AssistantChatRequest
from meety_ai.chat.agent import ChatGenerationError

logger = structlog.stdlib.get_logger(__name__)
assistant_router = APIRouter()


class AssistantNotReadyError(Exception):
    def __init__(self, ai_request_id: int):
        self.ai_request_id = ai_request_id


async def handle_not_ready(_: Request, error: AssistantNotReadyError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "aiRequestId": error.ai_request_id,
            "code": "generation_failed",
            "message": "전역 채팅 서버 설정이 준비되지 않았습니다.",
        },
    )


def require_ready(payload: AssistantChatRequest, request: Request) -> None:
    """생성기 경로는 첫 이벤트 전에도 응답이 시작되므로 설정 오류를 의존성에서 거른다."""
    if (
        request.app.state.assistant_model is None
        or request.app.state.settings.internal_api_key is None
    ):
        raise AssistantNotReadyError(payload.ai_request_id)


@assistant_router.post(
    "/v1/assistant/chat",
    response_class=EventSourceResponse,
    dependencies=[Depends(require_ready)],
)
async def stream_chat(
    payload: AssistantChatRequest, request: Request
) -> AsyncIterable[ServerSentEvent]:
    bind_contextvars(team_id=payload.team_id, ai_request_id=payload.ai_request_id)
    started = time.perf_counter()
    try:
        async for event, data in stream_answer(
            payload,
            request.app.state.assistant_model,
            request.app.state.backend_client,
            timeout_seconds=request.app.state.settings.assistant_timeout_seconds,
        ):
            yield ServerSentEvent(event=event, data=data)
    except ChatGenerationError as error:
        logger.warning(
            "assistant_chat_failed",
            code=error.code,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        yield ServerSentEvent(
            event="failed",
            data={"aiRequestId": payload.ai_request_id, "code": error.code, "message": str(error)},
        )
        return
    logger.info(
        "assistant_chat_completed", duration_ms=round((time.perf_counter() - started) * 1000)
    )
