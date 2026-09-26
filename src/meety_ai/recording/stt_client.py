"""Speechmatics 실시간 전사 연결을 관리한다."""

import asyncio
from collections.abc import Callable
from typing import Any

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    Model,
    ServerMessageType,
    TranscriptionConfig,
)


class STTProviderError(Exception):
    """STT 공급자 오류를 원본 응답 노출 없이 호출자에게 알린다."""


class SpeechmaticsClient:
    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[dict[str, Any]], None],
        on_error: Callable[[STTProviderError], None] | None = None,
    ) -> None:
        self._on_error = on_error
        self._error: STTProviderError | None = None
        self._closing = False
        self._finishing = False
        self._end_received = False
        self._client = AsyncClient(api_key=api_key)
        self._client.on(ServerMessageType.ADD_TRANSCRIPT, on_transcript)
        self._client.on(ServerMessageType.ERROR, self._handle_error)
        self._client.on(ServerMessageType.END_OF_TRANSCRIPT, self._handle_end)

    def _report_error(self) -> STTProviderError:
        if self._error is None:
            self._error = STTProviderError("음성 전사 공급자 연결 또는 처리에 실패했습니다.")
            if self._on_error is not None:
                self._on_error(self._error)
        return self._error

    def _handle_error(self, message: dict[str, Any]) -> None:
        self._report_error()

    def _handle_end(self, message: dict[str, Any]) -> None:
        self._end_received = True
        if not self._finishing:
            self._report_error()

    def _receive_done(self, task: asyncio.Task[None]) -> None:
        if not self._closing and not (self._finishing and self._end_received):
            self._report_error()

    async def start(self) -> None:
        """한국어 Enhanced 실시간 전사 세션을 시작한다."""
        try:
            await self._client.start_session(
                transcription_config=TranscriptionConfig(
                    language="ko",
                    model=Model.ENHANCED,
                    diarization="speaker",
                ),
                audio_format=AudioFormat(
                    encoding=AudioEncoding.PCM_S16LE,
                    sample_rate=16000,
                ),
            )
        except TimeoutError:
            raise
        except Exception as error:
            raise self._report_error() from error

        self._client._recv_task.add_done_callback(self._receive_done)
        if self._error is not None:
            raise self._error

    async def send_audio(self, chunk: bytes) -> None:
        """16 kHz mono pcm_s16le 청크를 전송한다."""
        try:
            await self._client.send_audio(chunk)
        except TimeoutError:
            raise
        except Exception as error:
            raise self._report_error() from error

    async def finish(self) -> None:
        """입력 종료를 알리고 마지막 확정 전사까지 기다린다."""
        self._finishing = True
        try:
            await self._client.stop_session()
        except TimeoutError:
            raise
        except Exception as error:
            raise self._report_error() from error
        if self._error is not None or not self._end_received:
            raise self._report_error()

    async def close(self) -> None:
        """오류나 연결 해제 시 전사 세션을 정리한다."""
        self._closing = True
        async with asyncio.timeout(10):
            await self._client.close()
