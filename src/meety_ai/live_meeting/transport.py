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


async def receive_message(
    websocket: WebSocket, failure: asyncio.Future[Exception], timeout: float | None
) -> dict:
    """입력 또는 background 오류를 기다리고 수신 작업이 남지 않도록 정리한다."""
    receiver = asyncio.create_task(websocket.receive())
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait((receiver, failure), return_when=asyncio.FIRST_COMPLETED)
        if failure.done():
            raise failure.result()
        return receiver.result()
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receiver
