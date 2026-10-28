"""Backend 전용 WebSocket의 연결 수를 제한하고 녹음 실행을 시작한다."""

from functools import partial

import structlog
from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from meety_ai.recording.connection import RecordingConnection
from meety_ai.stt.context import generate_stt_context

logger = structlog.stdlib.get_logger(__name__)

recording_router = APIRouter()


@recording_router.websocket("/v1/live-meeting")
async def start_recording_session(websocket: WebSocket) -> None:
    if (
        websocket.app.state.recording_connections
        >= websocket.app.state.settings.max_recording_connections
    ):
        logger.warning(
            "recording_rejected",
            reason="capacity_exceeded",
            active_connections=websocket.app.state.recording_connections,
        )
        await websocket.send_denial_response(
            JSONResponse(
                content={"error": "capacity_exceeded"},
                status_code=429,
            )
        )
        return

    state = websocket.app.state
    connection = RecordingConnection(
        websocket,
        state.stt_client_factory,
        partial(generate_stt_context, model=state.stt_context_model, client=state.backend_client),
        state.settings.stt_context_timeout_seconds,
    )
    websocket.app.state.recording_connections += 1
    try:
        await connection.run()
    finally:
        websocket.app.state.recording_connections -= 1
