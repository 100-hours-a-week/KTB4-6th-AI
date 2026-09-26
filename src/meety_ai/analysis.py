"""Analysis service entry point; feature routes will be added later."""

import os

from fastapi import FastAPI
from langchain_openai import ChatOpenAI

from meety_ai.app import create_app
from meety_ai.diarization.router import diarization_router
from meety_ai.settings import AnalysisSettings
from meety_ai.summary.chain import create_summary_chain
from meety_ai.summary.router import summary_router


def create_analysis_app() -> FastAPI:
    settings = AnalysisSettings()
    app = create_app("analysis", settings)
    # Modal SDK는 .env 파일이 아니라 환경 변수에서 토큰을 읽으므로 설정 값을 넘겨준다.
    if settings.modal_token_id and settings.modal_token_secret:
        os.environ["MODAL_TOKEN_ID"] = settings.modal_token_id
        os.environ["MODAL_TOKEN_SECRET"] = settings.modal_token_secret.get_secret_value()
    model = ChatOpenAI(
        model=settings.summary_model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=settings.summary_timeout_seconds,
        max_retries=0,
    )
    app.state.summary_chain = create_summary_chain(model)
    app.include_router(summary_router)
    app.include_router(diarization_router)
    return app
