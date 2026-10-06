"""연결별 질문 접수, 중복 응답과 답변 작업의 수명을 관리한다."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx
import structlog
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, TypeAdapter, ValidationError

from meety_ai.chat.agent import QnAGenerationError, generate_answer
from meety_ai.chat.schemas import (
    ChatAccepted,
    ChatCompleted,
    ChatCompletedPayload,
    ChatFailed,
    ChatFailedPayload,
    ChatRequest,
    Identifier,
)

logger = structlog.stdlib.get_logger(__name__)
MAX_CHAT_TEXT_SIZE = 256 * 1024
MAX_CHAT_REQUESTS = 128
identifier_adapter = TypeAdapter(Identifier)


class ChatSession:
    def __init__(
        self,
        model: BaseChatModel | None,
        backend_base_url: str | None,
        send_event: Callable[[BaseModel], Awaitable[None]],
        on_error: Callable[[Exception], None],
        *,
        timeout_seconds: float = 120,
        answer_generator: Callable[..., Awaitable[ChatCompletedPayload]] = generate_answer,
    ) -> None:
        self._model = model
        self._backend_base_url = backend_base_url
        self._send_event = send_event
        self._on_error = on_error
        self._timeout_seconds = timeout_seconds
        self._generate_answer = answer_generator
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # ponytail: 연결당 128개 결과만 보관한다. 재연결·영구 중복 방지는 Backend에서 관리한다.
        self._results: dict[str, ChatAccepted | ChatCompleted | ChatFailed] = {}

    async def submit(self, raw: dict, text_size: int, meeting_id: str | None) -> None:
        """질문을 접수한다. meeting_id가 없으면 녹음 흐름상 접수할 수 없는 상태다."""
        # 두 식별자까지 읽을 수 없으면 연결 오류 규칙을 적용한다.
        ids = {
            "meeting_id": identifier_adapter.validate_python(raw.get("meetingId")),
            "ai_request_id": identifier_adapter.validate_python(raw.get("aiRequestId")),
        }
        try:
            if text_size > MAX_CHAT_TEXT_SIZE:
                raise QnAGenerationError("invalid_request", "질문 메시지가 너무 큽니다.")
            event = ChatRequest.model_validate(raw)
            if self._closed or meeting_id is None or event.meeting_id != meeting_id:
                raise QnAGenerationError("invalid_request", "질문을 받을 수 없는 연결 상태입니다.")
            if event.ai_request_id in self._results:
                await self._send_event(self._results[event.ai_request_id])
                return
            if self._task is not None and not self._task.done():
                raise QnAGenerationError("invalid_request", "다른 질문을 처리 중입니다.")
            if len(self._results) >= MAX_CHAT_REQUESTS:
                raise QnAGenerationError("invalid_request", "연결의 질문 처리 한도를 초과했습니다.")
            if self._model is None or not self._backend_base_url:
                raise QnAGenerationError("internal_error", "질의응답 연동 설정이 없습니다.")
        except (ValidationError, QnAGenerationError) as error:
            code = error.code if isinstance(error, QnAGenerationError) else "invalid_request"
            message = (
                str(error)
                if isinstance(error, QnAGenerationError)
                else "질문 형식이 올바르지 않습니다."
            )
            await self._send_event(
                ChatFailed(
                    type="chat.failed",
                    **ids,
                    payload=ChatFailedPayload(code=code, message=message),
                )
            )
            return
        accepted = ChatAccepted(type="chat.accepted", **ids)
        await self._send_event(accepted)
        self._results[event.ai_request_id] = accepted
        self._task = asyncio.create_task(self._answer(event))

    async def _answer(self, event: ChatRequest) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=self._backend_base_url, timeout=30, follow_redirects=False
            ) as client:
                answer = await self._generate_answer(
                    event, self._model, client, timeout_seconds=self._timeout_seconds
                )
            result = ChatCompleted(
                type="chat.completed",
                meeting_id=event.meeting_id,
                ai_request_id=event.ai_request_id,
                payload=answer,
            )
        except QnAGenerationError as error:
            result = self._failed(event, error.code, str(error))
        except Exception as error:
            logger.warning("chat_failed", error_type=type(error).__name__)
            result = self._failed(event, "internal_error", "질문 처리에 실패했습니다.")
        self._results[event.ai_request_id] = result
        try:
            await self._send_event(result)
        except Exception as error:
            self._on_error(error)

    @staticmethod
    def _failed(event: ChatRequest, code: str, message: str) -> ChatFailed:
        return ChatFailed(
            type="chat.failed",
            meeting_id=event.meeting_id,
            ai_request_id=event.ai_request_id,
            payload=ChatFailedPayload(code=code, message=message),
        )

    async def close(self) -> None:
        """새 접수를 막고 진행 중 답변 작업의 취소 완료까지 기다린다."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
