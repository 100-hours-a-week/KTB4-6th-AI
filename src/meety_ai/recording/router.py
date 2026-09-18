"""Backend 전용 WebSocket의 수신·응답·연결 종료를 처리한다."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from meety_ai.recording.decoder import AudioDecodeError
from meety_ai.recording.schemas import (
    SessionEnded,
    SessionError,
    SessionErrorPayload,
    client_event_adapter,
)
from meety_ai.recording.session import RecordingSession, SessionProtocolError

recording_router = APIRouter()
MAX_TEXT_SIZE = 4 * 1024
MAX__RECORDING_CONNECTIONS = 20
BINARY_WAIT_TIMEOUT = 10


async def consume_pcm(chunk: bytes) -> None:
    # 향후 STT 연결
    return


@recording_router.websocket("/v1/live-meeting")
async def start_recording_session(websocket: WebSocket) -> None:
    if websocket.app.state.recording_connections >= MAX__RECORDING_CONNECTIONS:
        await websocket.send_denial_response(
            JSONResponse(
                content={"error": "capacity_exceeded"},
                status_code=429,
            )
        )
        return

    websocket.app.state.recording_connections += 1
    session = RecordingSession(consume_pcm)
    try:
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive()
                if data["type"] == "websocket.disconnect":
                    break

                request_id = None
                response = None
                if data.get("text") is not None:
                    if len(data["text"].encode("utf-8")) > MAX_TEXT_SIZE:
                        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")
                    event = client_event_adapter.validate_json(data["text"])
                    request_id = getattr(event, "request_id", None)
                    response = await session.handle_event(event)

                elif data.get("bytes") is not None:
                    await session.handle_binary(data["bytes"])

                if response is not None:
                    await websocket.send_json(response.model_dump(by_alias=True))

                if isinstance(response, SessionEnded):
                    await websocket.close(code=1000)
                    break

        except (ValidationError, SessionProtocolError, AudioDecodeError, TimeoutError) as error:
            if isinstance(error, ValidationError):
                code, message = "invalid_message", "메시지 형식이 올바르지 않습니다."
            elif isinstance(error, SessionProtocolError):
                code, message = error.code, str(error)
            elif isinstance(error, AudioDecodeError):
                code, message = "audio_decode_failed", "오디오 처리에 실패했습니다."
            else:
                code, message = "processing_timeout", "처리 대기 시간이 초과됐습니다."

            close_code = 1008
            if code == "message_too_large":
                close_code = 1009
            elif code in ("audio_decode_failed", "processing_timeout"):
                close_code = 1011

            response = SessionError(
                type="error",
                request_id=request_id,
                payload=SessionErrorPayload(code=code, message=message, retryable=False),
            )
            await websocket.send_json(response.model_dump(by_alias=True, exclude_none=True))
            await websocket.close(code=close_code)

    except WebSocketDisconnect:
        pass

    finally:
        try:
            await session.close()
        finally:
            websocket.app.state.recording_connections -= 1
