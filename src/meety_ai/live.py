"""실시간 앱을 조립하고 녹음 라우터를 등록한다."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial

import httpx
from fastapi import FastAPI
from langchain_openai import ChatOpenAI

from meety_ai.app import create_app
from meety_ai.recording.router import recording_router
from meety_ai.settings import LiveSettings
from meety_ai.stt.modal import ModalSTTClient


def create_live_app() -> FastAPI:
    settings = LiveSettings()
    app = create_app("live", settings)
    common_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        async with common_lifespan(app):
            async with httpx.AsyncClient(
                base_url=settings.backend_base_url,
                headers={"X-Internal-Api-Key": settings.internal_api_key.get_secret_value()},
                timeout=10,
            ) as backend_client:
                app.state.backend_client = backend_client
                yield

    app.router.lifespan_context = lifespan
    app.state.stt_context_model = ChatOpenAI(
        model=settings.stt_context_model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=settings.stt_context_timeout_seconds,
        max_retries=0,
    )
    app.include_router(recording_router)

    app.state.recording_connections = 0
    app.state.stt_client_factory = partial(
        ModalSTTClient,
        url=settings.modal_stt_url,
        proxy_token_id=settings.modal_proxy_token_id.get_secret_value(),
        proxy_token_secret=settings.modal_proxy_token_secret.get_secret_value(),
        start_timeout_seconds=settings.modal_stt_start_timeout_seconds,
    )
    return app
