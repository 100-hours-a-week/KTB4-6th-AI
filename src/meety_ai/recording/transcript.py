"""Speechmatics 결과를 문장 단위 Backend 메시지로 조립한다."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

from meety_ai.recording.schemas import (
    Message,
    SessionError,
    TranscriptCommitted,
    TranscriptCommittedPayload,
)

MAX_TRANSCRIPT_DURATION_SECONDS = 10
MESSAGE_SEND_TIMEOUT = 10
SENTENCE_ENDINGS = (".", "?", "!")
TranscriptMessage = dict[str, Any]
TranscriptQueue = asyncio.Queue[TranscriptMessage | None]


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
    meeting_id: str,
    recording_session_id: str,
) -> None:
    """확정 단어를 문장·화자·최대 길이 기준으로 묶어 전송한다."""
    sequence_number = 0
    buffered_text = ""
    started_at: float | None = None
    ended_at: float | None = None
    current_speaker: str | None = None

    async def flush() -> None:
        nonlocal sequence_number, buffered_text, started_at, ended_at, current_speaker
        if not buffered_text or started_at is None or ended_at is None:
            return

        transcript = TranscriptCommitted(
            type="transcript.committed",
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
            payload=TranscriptCommittedPayload(
                sequence_number=sequence_number,
                content=buffered_text.strip(),
                started_at_ms=round(started_at * 1000),
                ended_at_ms=round(ended_at * 1000),
                recognized_at=datetime.now(UTC),
            ),
        )
        await send_message(websocket, transcript)
        sequence_number += 1
        buffered_text = ""
        started_at = None
        ended_at = None
        current_speaker = None

    while True:
        message = await queue.get()
        if message is None:
            await flush()
            return

        results = message.get("results", [])
        for index, result in enumerate(results):
            alternatives = result.get("alternatives", [])
            if not alternatives:
                continue
            content = alternatives[0].get("content", "")
            if not content:
                continue

            speaker = alternatives[0].get("speaker")
            if (
                result.get("type") == "word"
                and current_speaker is not None
                and speaker != current_speaker
            ):
                await flush()

            if started_at is None:
                started_at = result["start_time"]
            ended_at = result["end_time"]
            current_speaker = speaker

            if result.get("type") == "punctuation":
                buffered_text = buffered_text.rstrip() + content
            else:
                buffered_text += (" " if buffered_text else "") + content

            next_is_punctuation = (
                index + 1 < len(results) and results[index + 1].get("type") == "punctuation"
            )
            if content.endswith(SENTENCE_ENDINGS):
                await flush()
            elif (
                ended_at - started_at >= MAX_TRANSCRIPT_DURATION_SECONDS and not next_is_punctuation
            ):
                await flush()
