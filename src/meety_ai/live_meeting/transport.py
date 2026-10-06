"""회의 연결의 메시지 직렬화와 전송 제한을 관리한다."""

import asyncio
import contextlib

from fastapi import WebSocket
from pydantic import BaseModel

from meety_ai.recording.schemas import SessionError

MESSAGE_SEND_TIMEOUT = 10


async def send_message(
    websocket: WebSocket, message: BaseModel, send_lock: asyncio.Lock | None = None
) -> None:
    """잠금 대기를 포함해 전송이 제한 시간을 넘으면 오류를 알린다."""
    async with asyncio.timeout(MESSAGE_SEND_TIMEOUT):
        async with send_lock if send_lock is not None else contextlib.nullcontext():
            await websocket.send_json(
                message.model_dump(
                    mode="json", by_alias=True, exclude_none=isinstance(message, SessionError)
                )
            )


class MeetingReceiver:
    """첫 background 오류로 입력 대기를 깨우고 수신 작업을 정리한다."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._failure: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()

    def report_error(self, error: Exception) -> None:
        """여러 작업이 실패해도 첫 오류만 전달한다."""
        if not self._failure.done():
            self._failure.set_result(error)

    async def receive(self, timeout: float | None = None) -> dict:
        """입력 또는 오류를 기다린다. 반환·실패·취소 시 수신 작업이 남지 않는다."""
        receiver = asyncio.create_task(self._websocket.receive())
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait((receiver, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure.done():
                raise self._failure.result()
            return receiver.result()
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver
