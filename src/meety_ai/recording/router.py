"""Backend 전용 WebSocket의 연결 수를 제한하고 녹음 실행을 시작한다."""

import structlog
from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from meety_ai.recording.connection import RecordingConnection

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

    connection = RecordingConnection(websocket, websocket.app.state.stt_client_factory)
    websocket.app.state.recording_connections += 1
    try:
        await connection.run()
    finally:
        websocket.app.state.recording_connections -= 1
