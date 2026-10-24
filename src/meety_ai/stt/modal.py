"""Modal WebSocket을 기존 STT 수명주기와 확정 전사 콜백에 연결한다."""

import asyncio
from collections.abc import Callable

from websockets.asyncio.client import connect

from meety_ai.modal_app.stt.protocol import (
    MAX_AUDIO_MESSAGE_BYTES,
    PipelineFailure,
    SegmentCommitted,
    SessionEnded,
    SessionReady,
    SessionStart,
    result_adapter,
)
from meety_ai.stt.client import STTProviderError
from meety_ai.stt.schemas import STTTranscript


class ModalSTTClient:
    def __init__(
        self,
        url: str,
        proxy_token_id: str,
        proxy_token_secret: str,
        on_transcript: Callable[[STTTranscript], None],
        on_error: Callable[[Exception], None] | None = None,
        start_timeout_seconds: float = 240,
    ):
        self._url = url
        self._headers = {"Modal-Key": proxy_token_id, "Modal-Secret": proxy_token_secret}
        self._on_transcript, self._on_error = on_transcript, on_error
        self._start_timeout = start_timeout_seconds
        self._websocket = None
        self._receiver: asyncio.Task | None = None
        self._error: STTProviderError | None = None
        self._closing = self._finishing = self._ended = False
        self._received_segments = self._sent_bytes = 0

    def _report_error(self) -> STTProviderError:
        if self._error is None:
            self._error = STTProviderError("Modal 전사 연결 또는 처리에 실패했습니다.")
            if self._on_error is not None:
                self._on_error(self._error)
        return self._error

    async def start(
        self,
        *,
        meeting_id: int = 0,
        recording_session_id: int = 0,
        context: str = "",
    ) -> None:
        try:
            async with asyncio.timeout(self._start_timeout):
                self._websocket = await connect(
                    self._url,
                    additional_headers=self._headers,
                    open_timeout=self._start_timeout,
                    max_size=256 * 1024,
                    max_queue=16,
                    close_timeout=5,
                )
                await self._websocket.send(
                    SessionStart(
                        type="session.start",
                        meeting_id=meeting_id,
                        recording_session_id=recording_session_id,
                        context=context,
                    ).model_dump_json(by_alias=True)
                )
                ready = result_adapter.validate_json(await self._websocket.recv())
                if not isinstance(ready, SessionReady):
                    raise self._report_error()
            self._receiver = asyncio.create_task(self._receive())
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except Exception:
            raise self._report_error() from None

    async def _receive(self) -> None:
        try:
            while True:
                result = result_adapter.validate_json(await self._websocket.recv())
                if isinstance(result, PipelineFailure):
                    raise self._report_error()
                if isinstance(result, SegmentCommitted):
                    if (
                        result.segment_id != self._received_segments
                        or result.ended_at_ms > self._sent_bytes * 1000 // 32000
                    ):
                        raise self._report_error()
                    self._on_transcript(
                        STTTranscript(
                            content=result.content,
                            speaker_id=result.speaker_id,
                            started_at_ms=result.started_at_ms,
                            ended_at_ms=result.ended_at_ms,
                        )
                    )
                    self._received_segments += 1
                elif isinstance(result, SessionEnded):
                    if (
                        not self._finishing
                        or result.succeeded_segments != self._received_segments
                        or result.received_samples != self._sent_bytes // 2
                        or result.audio_duration_ms != self._sent_bytes * 1000 // 32000
                        or result.unscored_tail_samples > result.received_samples
                    ):
                        raise self._report_error()
                    self._ended = True
                    return
                else:
                    raise self._report_error()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closing:
                self._report_error()

    async def _send(self, message: str | bytes) -> None:
        if self._error is not None:
            raise self._error
        try:
            await self._websocket.send(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise self._report_error() from None

    async def send_audio(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % 2 or len(chunk) > MAX_AUDIO_MESSAGE_BYTES:
            raise STTProviderError("PCM 청크의 크기 또는 sample 경계가 잘못됐습니다.")
        self._sent_bytes += len(chunk)
        await self._send(chunk)

    async def finish(self) -> None:
        self._finishing = True
        await self._send('{"type":"session.stop"}')
        await self._receiver
        if self._error is not None or not self._ended:
            raise self._report_error()

    async def close(self) -> None:
        self._closing = True
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        if self._websocket is not None:
            await self._websocket.close()
