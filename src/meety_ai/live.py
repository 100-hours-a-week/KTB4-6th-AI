"""실시간 앱을 조립하고 녹음 라우터를 등록한다."""

from fastapi import FastAPI

from meety_ai.app import create_app
from meety_ai.recording.router import recording_router


def create_live_app() -> FastAPI:
    app = create_app("live")
    app.include_router(recording_router)

    app.state.recording_connections = 0
    return app
