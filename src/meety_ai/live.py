"""실시간 앱을 조립하고 녹음 라우터를 등록한다."""

from functools import partial

from fastapi import FastAPI

from meety_ai.app import create_app
from meety_ai.recording.router import recording_router
from meety_ai.settings import LiveSettings
from meety_ai.stt.modal import ModalSTTClient


def create_live_app() -> FastAPI:
    app = create_app("live", LiveSettings())
    app.include_router(recording_router)

    app.state.recording_connections = 0
    app.state.stt_client_factory = partial(
        ModalSTTClient,
        url=app.state.settings.modal_stt_url,
        proxy_token_id=app.state.settings.modal_proxy_token_id.get_secret_value(),
        proxy_token_secret=app.state.settings.modal_proxy_token_secret.get_secret_value(),
        start_timeout_seconds=app.state.settings.modal_stt_start_timeout_seconds,
    )
    return app
