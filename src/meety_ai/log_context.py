"""HTTP 요청과 WebSocket 연결별로 로그 문맥을 분리한다."""

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    get_contextvars,
)

logger = structlog.stdlib.get_logger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _http_request_id(scope: Scope) -> str:
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"x-request-id"]
    if len(values) == 1:
        value = values[0].decode("latin-1")
        if _REQUEST_ID_PATTERN.fullmatch(value):
            return value
    # 누락·잘못된 형식·중복 헤더는 원문을 기록하지 않고 대체 식별자를 만든다.
    return uuid4().hex


@contextmanager
def bind_request_context(request_id: str, *, meeting_id: str | None = None) -> Iterator[None]:
    """검증된 작업 메시지의 요청 ID를 연결하고 완료·실패 시 이전 문맥을 복원한다."""
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("request_id는 영문·숫자·점·밑줄·하이픈으로 된 1~128자여야 합니다.")
    context = {"request_id": request_id}
    if meeting_id is not None:
        context["meeting_id"] = meeting_id
    with bound_contextvars(**context):
        yield


class LogContextMiddleware:
    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        previous = get_contextvars()
        clear_contextvars()
        if scope["type"] == "http":
            request_id = _http_request_id(scope)
            bind_contextvars(service=self.service, request_id=request_id)
        else:
            # 연결 식별자와 개별 질문·작업의 요청 식별자는 구분한다.
            bind_contextvars(service=self.service, connection_id=uuid4().hex)

        started = time.perf_counter()
        status = 500  # 응답을 시작하지 못하고 예외로 끝나면 서버 오류로 기록한다.

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if scope["type"] == "http" and message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 헬스체크는 주기적으로 반복돼 로그 비용만 늘리므로 제외한다.
            # 쿼리 문자열에는 비밀값이 섞일 수 있어 path만 남긴다.
            if scope["type"] == "http" and scope["path"] != "/healthz":
                logger.info(
                    "http_request_completed",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            clear_contextvars()
            bind_contextvars(**previous)
