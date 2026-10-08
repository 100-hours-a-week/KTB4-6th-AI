import pytest

from meety_ai.recording.schemas import (
    AudioMeta,
    AudioMetaPayload,
    DecoderReset,
    DecoderResetPayload,
    SessionPause,
    SessionResume,
    SessionStart,
    SessionStop,
)
from meety_ai.recording.session import RecordingSession, SessionProtocolError

# 스트림 첫 8바이트 헤더 검사를 통과하도록 가짜 청크 앞에 붙이는 webm EBML magic
WEBM_HEAD = b"\x1a\x45\xdf\xa3"


def reset(request_id):
    return DecoderReset(
        type="decoder.reset",
        request_id=request_id,
        payload=DecoderResetPayload(audio_format="webm_opus"),
    )


# 테스트 이름: test_recording_lifecycle
# 목적: 시작·정지·재개·종료가 하나의 녹음 흐름으로 이어지는지 확인한다.
# 준비: 소비자에게 전달한 PCM을 기록하고, finish에서도 잔여 PCM을 내는 디코더.
# 실행: start → reset → meta(0) → binary → pause → resume → reset → meta(1) → binary → stop.
# 기대 결과: ready의 ID·출력 규격이 맞고 pause/resume 응답이 반환된다.
# 기대 결과: 재개 후 sequence가 이어지고 ended의 lastSequence는 1이다.
# 기대 결과: 두 디코더의 finish 잔여 PCM까지 합산한다. 총 64000 bytes이므로 2000ms이다.
# 기대 결과: 종료 응답 전에 자원이 정리되고, 종료 뒤 새 meta는 invalid_state이다.
async def test_recording_lifecycle(fake_decoders):
    received = []

    async def on_pcm(chunk):
        received.append(chunk)

    session = RecordingSession(on_pcm)
    try:
        ready = await session.handle_event(
            SessionStart(
                type="session.start",
                request_id="start-01",
                meeting_id=1,
                recording_session_id=1,
            )
        )
        assert ready.request_id == "start-01"
        assert ready.meeting_id == 1
        assert ready.recording_session_id == 1
        assert ready.payload.output_audio_format == "pcm_s16le"
        assert ready.payload.output_sample_rate_hz == 16000
        assert ready.payload.output_channels == 1

        decoder_ready = await session.handle_event(reset("reset-01"))
        assert decoder_ready.request_id == "reset-01"
        assert decoder_ready.payload.input_audio_format == "webm_opus"

        assert (
            await session.handle_event(
                AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=0))
            )
            is None
        )
        await session.handle_binary(WEBM_HEAD + b"compressed chunk")

        paused = await session.handle_event(
            SessionPause(type="session.pause", request_id="pause-01")
        )
        assert paused.payload.status == "PAUSED"

        resumed = await session.handle_event(
            SessionResume(type="session.resume", request_id="resume-01")
        )
        assert resumed.payload.status == "READY"
        await session.handle_event(reset("reset-02"))

        assert (
            await session.handle_event(
                AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=1))
            )
            is None
        )
        await session.handle_binary(WEBM_HEAD + b"compressed chunk 2")

        ended = await session.handle_event(SessionStop(type="session.stop", request_id="stop-01"))
        assert ended.payload.last_sequence == 1
        assert ended.payload.audio_duration_ms == 2000
        assert received == [b"\0" * 16000] * 4
        assert len(fake_decoders) == 2
        assert fake_decoders[0].fed == [WEBM_HEAD + b"compressed chunk"]
        assert fake_decoders[1].fed == [WEBM_HEAD + b"compressed chunk 2"]
        assert all(decoder.closed for decoder in fake_decoders)

        with pytest.raises(SessionProtocolError) as error:
            await session.handle_event(
                AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=2))
            )
        assert error.value.code == "invalid_state"
    finally:
        await session.close()


# 테스트 이름: test_invalid_state_transition
# 목적: 시작 전 또는 일시 정지 중 허용되지 않는 입력을 차단한다.
# 준비: 기존 fake_decoders와 새 RecordingSession을 사례마다 사용한다.
# 실행: 시작 전 stop, start → pause → meta, start → pause → binary를 각각 수행한다.
# 기대 결과: SessionProtocolError.code가 invalid_state이고 디코더 입력은 없다.
# 실패 조건: 요청이 허용되거나 잘못된 오류로 거부되거나 음성이 디코더에 전달된다.
# 주의: 종료 후 입력 거부는 위 test_recording_lifecycle에서 이미 검증한다.
# 주의: 실패해도 finally에서 세션을 정리한다.
# 핵심 API: async def 테스트(pytest-asyncio auto 모드), pytest.mark.parametrize, pytest.raises.
async def test_invalid_state_transition(fake_decoders):
    received = []

    async def on_pcm(chunk):
        received.append(chunk)

    session_1 = RecordingSession(on_pcm)
    try:
        with pytest.raises(SessionProtocolError) as error:
            await session_1.handle_event(SessionStop(type="session.stop", request_id="stop-01"))
        assert error.value.code == "invalid_state"
        assert fake_decoders == []
        assert received == []
    finally:
        await session_1.close()

    session_2 = RecordingSession(on_pcm)
    try:
        await session_2.handle_event(
            SessionStart(
                type="session.start",
                request_id="start-01",
                meeting_id=1,
                recording_session_id=1,
            )
        )
        paused = await session_2.handle_event(
            SessionPause(type="session.pause", request_id="pause-01")
        )
        assert paused.payload.status == "PAUSED"
        with pytest.raises(SessionProtocolError) as error:
            await session_2.handle_event(
                AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=0))
            )
        assert error.value.code == "invalid_state"
        assert fake_decoders == []
        assert received == []
    finally:
        await session_2.close()

    session_3 = RecordingSession(on_pcm)
    try:
        await session_3.handle_event(
            SessionStart(
                type="session.start",
                request_id="start-01",
                meeting_id=1,
                recording_session_id=1,
            )
        )
        paused = await session_3.handle_event(
            SessionPause(type="session.pause", request_id="pause-01")
        )
        assert paused.payload.status == "PAUSED"
        with pytest.raises(SessionProtocolError) as error:
            await session_3.handle_binary(b"audio chunk")
        assert error.value.code == "invalid_state"
        assert fake_decoders == []
        assert received == []
    finally:
        await session_3.close()
