"""Speechmatics 실시간 전사 연결을 관리한다."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    Model,
    ServerMessageType,
    TranscriptionConfig,
)

from meety_ai.stt.client import STTProviderError
from meety_ai.stt.schemas import SpeakerProfile, STTTranscript

MAX_TRANSCRIPT_DURATION_SECONDS = 10
SENTENCE_ENDINGS = (".", "?", "!")


class SpeechmaticsClient:
    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[STTTranscript], None],
        on_error: Callable[[STTProviderError], None] | None = None,
    ) -> None:
        self._on_transcript = on_transcript
        self._on_error = on_error
        self._buffered_text = ""
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._current_speaker: str | None = None
        self._error: STTProviderError | None = None
        self._closing = False
        self._finishing = False
        self._end_received = False
        self._client = AsyncClient(api_key=api_key)
        self._client.on(ServerMessageType.ADD_TRANSCRIPT, self._handle_transcript)
        self._client.on(ServerMessageType.ERROR, self._handle_error)
        self._client.on(ServerMessageType.END_OF_TRANSCRIPT, self._handle_end)

    def _report_error(self, cause: Exception | None = None) -> STTProviderError:
        if self._error is None:
            self._error = STTProviderError("음성 전사 공급자 연결 또는 처리에 실패했습니다.")
            # 콜백에서 즉시 보고하므로 원본 예외를 알림 전에 연결한다.
            self._error.__cause__ = cause
            if self._on_error is not None:
                self._on_error(self._error)
        return self._error

    def _flush_transcript(self) -> None:
        if not self._buffered_text or self._started_at is None or self._ended_at is None:
            return
        self._on_transcript(
            STTTranscript(
                content=self._buffered_text.strip(),
                started_at_ms=round(self._started_at * 1000),
                ended_at_ms=round(self._ended_at * 1000),
            )
        )
        self._buffered_text = ""
        self._started_at = None
        self._ended_at = None
        self._current_speaker = None

    def _handle_transcript(self, message: dict[str, Any]) -> None:
        """확정 단어를 문장·화자·최대 길이 기준으로 묶는다."""
        try:
            results = message.get("results", [])
            for index, result in enumerate(results):
                alternatives = result.get("alternatives", [])
                if not alternatives:
                    continue
                content = alternatives[0].get("content", "")
                if not content:
                    continue

                speaker = alternatives[0].get("speaker")
                if (
                    result.get("type") == "word"
                    and self._current_speaker is not None
                    and speaker != self._current_speaker
                ):
                    self._flush_transcript()

                if self._started_at is None:
                    self._started_at = result["start_time"]
                self._ended_at = result["end_time"]
                self._current_speaker = speaker

                if result.get("type") == "punctuation":
                    self._buffered_text = self._buffered_text.rstrip() + content
                else:
                    self._buffered_text += (" " if self._buffered_text else "") + content

                next_is_punctuation = (
                    index + 1 < len(results) and results[index + 1].get("type") == "punctuation"
                )
                if content.endswith(SENTENCE_ENDINGS):
                    self._flush_transcript()
                elif (
                    self._ended_at - self._started_at >= MAX_TRANSCRIPT_DURATION_SECONDS
                    and not next_is_punctuation
                ):
                    self._flush_transcript()
        except Exception as error:
            self._report_error(error)

    def _handle_error(self, message: dict[str, Any]) -> None:
        self._report_error()

    def _handle_end(self, message: dict[str, Any]) -> None:
        self._end_received = True
        if not self._finishing:
            self._report_error()
        else:
            try:
                self._flush_transcript()
            except Exception as error:
                self._report_error(error)

    def _receive_done(self, task: asyncio.Task[None]) -> None:
        if not self._closing and not (self._finishing and self._end_received):
            self._report_error()

    async def start(
        self,
        *,
        meeting_id: int = 0,
        recording_session_id: int = 0,
        context: str = "",
        profiles: Sequence[SpeakerProfile] = (),
    ) -> None:
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
            raise self._report_error(error) from error

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
            raise self._report_error(error) from error

    async def finish(self) -> None:
        """입력 종료를 알리고 마지막 확정 전사까지 기다린다."""
        self._finishing = True
        try:
            await self._client.stop_session()
        except TimeoutError:
            raise
        except Exception as error:
            raise self._report_error(error) from error
        if self._error is not None or not self._end_received:
            raise self._report_error()

    async def close(self) -> None:
        """오류나 연결 해제 시 전사 세션을 정리한다."""
        self._closing = True
        async with asyncio.timeout(10):
            await self._client.close()
