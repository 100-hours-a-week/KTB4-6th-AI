"""개발·부하테스트용 가짜 외부 공급자. 비용 없이 공급자를 뺀 나머지 경로를 실행한다.

운영 진입점 대신 이 모듈의 앱 팩토리로 서버를 띄운다.
    uvicorn meety_ai.fake_providers:create_live_app --factory
    uvicorn meety_ai.fake_providers:create_analysis_app --factory
응답 지연은 FAKE_*_LATENCY_MS로 공급자마다 실제와 비슷하게 맞춘다.
"""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any

from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from meety_ai import analysis, live
from meety_ai.summary.chain import create_summary_chain

# 16 kHz mono pcm_s16le 1초 분량.
PCM_BYTES_PER_SECOND = 16000 * 2
FAKE_SUMMARY = "# 회의 요약\n\n- 가짜 공급자가 생성한 요약입니다."


class FakeSpeechmaticsClient:
    """오디오 1초마다 확정 문장 하나를 지연 후 돌려주는 가짜 STT 클라이언트."""

    def __init__(
        self,
        api_key: str,
        on_transcript: Callable[[dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
        latency_seconds: float = 0.0,
    ) -> None:
        self._on_transcript = on_transcript
        self._latency = latency_seconds
        self._pcm_bytes = 0
        self._emitted_seconds = 0
        self._pending: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await asyncio.sleep(self._latency)

    async def _emit_transcript(self, message: dict[str, Any]) -> None:
        await asyncio.sleep(self._latency)
        self._on_transcript(message)

    async def send_audio(self, chunk: bytes) -> None:
        self._pcm_bytes += len(chunk)
        while (self._emitted_seconds + 1) * PCM_BYTES_PER_SECOND <= self._pcm_bytes:
            start = self._emitted_seconds
            self._emitted_seconds += 1
            message = {
                "message": "AddTranscript",
                "results": [
                    {
                        "type": "word",
                        "start_time": start,
                        "end_time": start + 1,
                        "alternatives": [{"content": "테스트 발화입니다.", "speaker": "S1"}],
                    }
                ],
            }
            task = asyncio.create_task(self._emit_transcript(message))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def finish(self) -> None:
        # 지연 중인 전사가 모두 큐에 들어간 뒤 종료를 알린다.
        await asyncio.gather(*self._pending)

    async def close(self) -> None:
        for task in self._pending:
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()


async def fake_diarize(
    audio_url: str, timeout_seconds: float, latency_seconds: float = 0.0
) -> list[dict[str, int]]:
    """화자 두 명이 5초씩 번갈아 말한 1시간 분량의 화자 구간을 돌려준다."""
    await asyncio.sleep(latency_seconds)
    return [
        {"speaker_id": index % 2, "start_ms": index * 5000, "end_ms": (index + 1) * 5000}
        for index in range(720)
    ]


def fake_chat_model(latency_seconds: float) -> Runnable:
    """고정 요약을 돌려주는 가짜 LLM.

    FakeListChatModel은 비동기 호출에서도 time.sleep을 스레드풀에서 실행해
    동시 요청 수가 스레드 수에 묶이므로 asyncio.sleep으로 직접 지연한다.
    """

    async def respond(prompt: Any) -> AIMessage:
        await asyncio.sleep(latency_seconds)
        return AIMessage(content=FAKE_SUMMARY)

    return RunnableLambda(respond)


class FakeLatencySettings(BaseSettings):
    """가짜 공급자별 응답 지연.

    운영 Settings가 MEETY_ 접두사의 미정의 항목을 거부하므로 FAKE_ 접두사를 쓴다.
    """

    model_config = SettingsConfigDict(env_prefix="FAKE_", env_file=".env", extra="ignore")

    stt_latency_ms: int = Field(default=0, ge=0)
    summary_latency_ms: int = Field(default=0, ge=0)
    diarization_latency_ms: int = Field(default=0, ge=0)


def _forbid_production(app: FastAPI) -> None:
    if app.state.settings.environment == "production":
        raise RuntimeError("운영 환경에서는 가짜 공급자를 켤 수 없습니다.")


def create_live_app() -> FastAPI:
    app = live.create_live_app()
    _forbid_production(app)
    latency = FakeLatencySettings()
    app.state.stt_client_factory = partial(
        FakeSpeechmaticsClient, latency_seconds=latency.stt_latency_ms / 1000
    )
    return app


def create_analysis_app() -> FastAPI:
    app = analysis.create_analysis_app()
    _forbid_production(app)
    latency = FakeLatencySettings()
    app.state.summary_chain = create_summary_chain(
        fake_chat_model(latency.summary_latency_ms / 1000)
    )
    app.state.diarize = partial(fake_diarize, latency_seconds=latency.diarization_latency_ms / 1000)
    return app
