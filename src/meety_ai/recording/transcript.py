"""공통 전사 구간과 화자 연결 변경을 Backend 메시지로 전송한다."""

import asyncio
from datetime import UTC, datetime

from fastapi import WebSocket

from meety_ai.core.schemas import MeetingId, RecordingSessionId
from meety_ai.recording.schemas import (
    Message,
    SessionError,
    SpeakerMappingUpdated,
    SpeakerMappingUpdatedPayload,
    TranscriptCommitted,
    TranscriptCommittedPayload,
)
from meety_ai.stt.schemas import STTSpeakerMapping, STTTranscript

MESSAGE_SEND_TIMEOUT = 10
TranscriptQueue = asyncio.Queue[STTTranscript | STTSpeakerMapping | None]


async def send_message(websocket: WebSocket, message: Message) -> None:
    """Backend 전송이 일정 시간 이상 멈추면 세션을 중단한다."""
    async with asyncio.timeout(MESSAGE_SEND_TIMEOUT):
        await websocket.send_json(
            message.model_dump(
                mode="json", by_alias=True, exclude_none=isinstance(message, SessionError)
            )
        )


async def send_transcripts(
    websocket: WebSocket,
    queue: TranscriptQueue,
    meeting_id: MeetingId,
    recording_session_id: RecordingSessionId,
    start_sequence: int = 0,
    transcript_sequence_start: int = 0,
) -> None:
    """확정 전사에 회의 식별자·전사 번호·재연결 시간 오프셋을 붙여 수신 순서대로 전송한다.

    화자 매핑 변경은 같은 순서로 보내되 전사 번호를 소비하지 않는다.
    """
    sequence_number = transcript_sequence_start
    # shortcut: 짧은 청크의 오차를 허용하며, 정확한 복구가 필요하면 누적 샘플 위치를 전달한다.
    time_offset_ms = start_sequence * 100
    while (segment := await queue.get()) is not None:
        if isinstance(segment, STTSpeakerMapping):
            await send_message(
                websocket,
                SpeakerMappingUpdated(
                    type="speaker.mapping.updated",
                    meeting_id=meeting_id,
                    recording_session_id=recording_session_id,
                    payload=SpeakerMappingUpdatedPayload(mappings=segment.mappings),
                ),
            )
            continue
        transcript = TranscriptCommitted(
            type="transcript.committed",
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
            payload=TranscriptCommittedPayload(
                sequence_number=sequence_number,
                content=segment.content,
                started_at_ms=time_offset_ms + segment.started_at_ms,
                ended_at_ms=time_offset_ms + segment.ended_at_ms,
                speaker_id=segment.speaker_id,
                recognized_at=datetime.now(UTC),
            ),
        )
        await send_message(websocket, transcript)
        sequence_number += 1
