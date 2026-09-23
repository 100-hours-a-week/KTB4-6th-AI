"""Analysis service entry point; feature routes will be added later."""

from fastapi import FastAPI
from langchain_openai import ChatOpenAI

from meety_ai.app import create_app
from meety_ai.settings import AnalysisSettings
from meety_ai.summary.chain import create_summary_chain
from meety_ai.summary.router import summary_router


def create_analysis_app() -> FastAPI:
    settings = AnalysisSettings()
    app = create_app("analysis", settings)
    model = ChatOpenAI(
        model=settings.summary_model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=settings.summary_timeout_seconds,
    )
    app.state.summary_chain = create_summary_chain(model)
    app.include_router(summary_router)
    return app
