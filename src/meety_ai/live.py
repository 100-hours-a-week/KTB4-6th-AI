"""실시간 앱을 조립하고 녹음 라우터를 등록한다."""

from functools import partial

from fastapi import FastAPI

from meety_ai.app import create_app
from meety_ai.recording.router import recording_router
from meety_ai.settings import LiveSettings
from meety_ai.stt.speechmatics import SpeechmaticsClient


def create_live_app() -> FastAPI:
    app = create_app("live", LiveSettings())
    app.include_router(recording_router)

    app.state.recording_connections = 0
    app.state.stt_client_factory = partial(
        SpeechmaticsClient, api_key=app.state.settings.speechmatics_api_key.get_secret_value()
    )
    return app
