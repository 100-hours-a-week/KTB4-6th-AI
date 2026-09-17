# 대상: recording/session.py의 상태·순서·수명주기
# 작성 순서: 정상 흐름 → 순서 오류 → 경계값 → 종료·실패
# 공통 규격: 테스트 이름 / 목적 / 준비 / 실행 / 기대 결과
# 디코더 경계만 작은 가짜 객체로 대체해 PCM 출력·지연·실패를 제어한다.
# handle_event(), handle_binary(), close()로 실행하고 응답·오류·정리를 확인한다.
# 내부 _state 값을 직접 검사하기보다 다음 요청의 허용·거부로 상태를 확인한다.
# 아직 구현하지 않은 테스트는 아래 주석을 따라 순차적으로 작성한다.

import asyncio

import pytest

import meety_ai.recording.session as session_module
from meety_ai.recording.schemas import (
    AudioMeta,
    AudioMetaPayload,
    SessionPause,
    SessionResume,
    SessionStart,
    SessionStartPayload,
    SessionStop,
)
from meety_ai.recording.session import RecordingSession, SessionProtocolError


@pytest.fixture
def fake_decoders(monkeypatch):
    instances = []

    class FakeDecoder:
        def __init__(self, on_pcm):
            self.on_pcm = on_pcm
            self.fed = []
            self.closed = False
            instances.append(self)

        async def start(self):
            pass

        async def feed(self, chunk):
            self.fed.append(chunk)
            await self.on_pcm(b"\0" * 16000)

        async def finish(self):
            await self.on_pcm(b"\0" * 16000)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(session_module, "AudioDecoder", FakeDecoder)
    return instances


# 테스트 이름: test_recording_lifecycle
# 목적: 시작·정지·재개·종료가 하나의 녹음 흐름으로 이어지는지 확인한다.
# 준비: 소비자에게 전달한 PCM을 기록하고, finish에서도 잔여 PCM을 내는 디코더.
# 실행: start → meta(0) → binary → pause → resume → meta(1) → binary → stop.
# 기대 결과: ready의 ID·출력 규격이 맞고 pause/resume 응답이 반환된다.
# 기대 결과: 재개 후 sequence가 이어지고 ended의 lastSequence는 1이다.
# 기대 결과: finish의 잔여 PCM까지 합산한다. 총 48000 bytes이므로 1500ms이다.
# 기대 결과: 종료 응답 전에 자원이 정리되고, 종료 뒤 새 meta는 invalid_state이다.


def test_recording_lifecycle(fake_decoders):
    received = []

    async def on_pcm(chunk):
        received.append(chunk)

    async def scenario():
        session = RecordingSession(on_pcm)
        try:
            ready = await session.handle_event(
                SessionStart(
                    type="session.start",
                    request_id="start-01",
                    meeting_id="meeting-01",
                    recording_session_id="record-01",
                    payload=SessionStartPayload(audio_format="webm_opus"),
                )
            )
            assert ready.request_id == "start-01"
            assert ready.meeting_id == "meeting-01"
            assert ready.recording_session_id == "record-01"
            assert ready.payload.output_audio_format == "pcm_s16le"
            assert ready.payload.output_sample_rate_hz == 16000
            assert ready.payload.output_channels == 1

            assert (
                await session.handle_event(
                    AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=0))
                )
                is None
            )
            await session.handle_binary(b"compressed chunk")

            paused = await session.handle_event(
                SessionPause(type="session.pause", request_id="pause-01")
            )
            assert paused.payload.status == "PAUSED"

            resumed = await session.handle_event(
                SessionResume(type="session.resume", request_id="resume-01")
            )
            assert resumed.payload.status == "READY"

            assert (
                await session.handle_event(
                    AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=1))
                )
                is None
            )
            await session.handle_binary(b"compressed chunk 2")

            ended = await session.handle_event(
                SessionStop(type="session.stop", request_id="stop-01")
            )
            assert ended.payload.last_sequence == 1
            assert ended.payload.audio_duration_ms == 1500
            assert received == [b"\0" * 16000] * 3
            assert len(fake_decoders) == 1
            assert fake_decoders[0].fed == [b"compressed chunk", b"compressed chunk 2"]
            assert fake_decoders[0].closed

            with pytest.raises(SessionProtocolError) as error:
                await session.handle_event(
                    AudioMeta(type="audio.meta", payload=AudioMetaPayload(sequence=2))
                )
            assert error.value.code == "invalid_state"
        finally:
            await session.close()

    asyncio.run(scenario())


# 테스트 이름: test_invalid_state_transition
# 목적: 현재 상태에서 허용되지 않는 제어·음성 입력을 막는다.
# 준비: 각 사례에 맞는 상태를 공개 메서드 호출로 만든다.
# 실행: 시작 전 stop, 중복 start, READY에서 resume, PAUSED에서 meta 또는 binary.
# 기대 결과: SessionProtocolError의 code가 invalid_state이다.
# 주의: 오류 후 같은 연결에서 복구하지 않으므로 사례마다 새 세션을 사용한다.

# 테스트 이름: test_meta_binary_pairing
# 목적: meta 하나와 binary 하나가 반드시 짝을 이루도록 한다.
# 준비: 시작된 세션. 뒤에 JSON을 보내는 사례는 meta(0)까지 전달한다.
# 실행: meta 없이 binary, 또는 meta 이후 binary 대신 제어 이벤트를 전달한다.
# 기대 결과: 전자는 unexpected_binary, 후자는 expected_binary이다.
# 사례: meta 뒤의 start, meta, pause, resume, stop을 각각 확인한다.

# 테스트 이름: test_sequence_continuity
# 목적: 누락·중복 청크를 감지한다.
# 준비: 시작된 세션 또는 meta(0) → binary를 완료한 세션.
# 실행: 첫 sequence로 1을 보내거나, 처리 완료 후 0 또는 2를 보낸다.
# 기대 결과: invalid_sequence이다. 각 사례는 별도 세션에서 실행한다.
# 주의: meta(0) → meta(0)은 sequence 중복이 아니라 binary 순서 위반이다.

# 테스트 이름: test_binary_size_boundary
# 목적: 빈 청크와 크기 상한을 검사한다.
# 준비: 각 사례마다 start → meta(0)까지 전달한다.
# 실행: 0 bytes, 256 KiB, 256 KiB + 1 byte를 각각 전달한다.
# 기대 결과: invalid_message, 정상 전달, message_too_large 순서로 처리된다.
# 기대 결과: 거부된 입력은 디코더에 전달되지 않는다.

# 테스트 이름: test_empty_session_stop
# 목적: 음성 청크가 없는 녹음을 정상 종료한다.
# 준비: 시작 후 binary를 전달하지 않은 세션.
# 실행: READY에서 stop 또는 pause 후 stop을 각각 실행한다.
# 기대 결과: ended의 lastSequence는 None, audioDurationMs는 0이다.
# 기대 결과: PCM 입력을 요구하는 finish를 호출하지 않고 디코더를 정리한다.

# 테스트 이름: test_processing_timeout
# 목적: 디코더 입력·정상 종료가 무한히 대기하지 않게 한다.
# 준비: feed 또는 finish가 대기하는 디코더와 테스트용 짧은 제한시간.
# 실행: 유효한 meta 뒤 binary 전달, 또는 binary 처리 후 stop을 실행한다.
# 기대 결과: processing_timeout이다. stop의 시간 초과 후에는 정리도 완료된다.
# 주의: feed 실패 후에는 호출자가 finally에서 close()를 호출해야 한다.

# 테스트 이름: test_stop_failure_or_cancellation_cleanup
# 목적: 종료 중 실패하거나 취소돼도 디코더 자원을 회수한다.
# 준비: binary를 처리했고 finish가 실패하거나 취소를 기다리는 디코더.
# 실행: stop을 호출하거나, 실행 중인 stop 작업을 취소하고 완료까지 기다린다.
# 기대 결과: 원래 실패·취소가 전달되고, 정상 ended 응답 없이 자원이 정리된다.

# 테스트 이름: test_close_on_disconnect
# 목적: 연결 해제 시 정리하고 중복 정리에도 안전하게 동작한다.
# 준비: 시작된 세션과 자원 정리 여부를 관찰할 수 있는 디코더.
# 실행: close()를 호출하고 다시 호출한다.
# 기대 결과: 자원이 회수되고 반복 호출도 오류 없이 끝난다.
