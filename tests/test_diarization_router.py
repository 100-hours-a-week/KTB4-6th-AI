from fastapi.testclient import TestClient

import meety_ai.diarization.router as router_module
from meety_ai.app import create_app
from meety_ai.diarization.router import diarization_router
from meety_ai.settings import Settings


# 테스트 이름: test_diarization_route_attributes_speakers_from_modal_intervals
# 목적: Backend가 전사 구간과 presigned URL을 보내면, Modal이 준 화자 구간으로
#       전사 구간마다 대표 화자가 붙은 응답을 받는 핵심 흐름을 지킨다.
#       기존 전사 구간(segmentId·content·시각)이 그대로 유지되는지도 함께 확인한다.
# 준비: create_app("analysis", Settings(_env_file=None))에 diarization_router를 붙인다.
#       router 모듈이 쓰는 modal.Function.from_name을 가짜로 바꾼다.
#       가짜는 계약 형식의 성공 결과를 돌려준다:
#       {"status": "ok", "intervals": [화자 0 0~2000ms, 화자 1 1500~5000ms]}
#       겹치는 구간을 일부러 넣는다. 받은 audio_url도 기록해 둔다.
# 실행: POST /v1/diarization, camelCase 본문으로
#       requestId, meetingId, audioUrl("https://...?X-Amz-Signature=..."),
#       segments 2개(0~1800ms, 2000~5000ms)를 보낸다.
# 기대 결과: 200 응답, meetingId가 그대로 오고,
#       segments[0].speakerId == 0, segments[1].speakerId == 1이며
#       나머지 필드는 요청과 같다. 가짜 Modal 함수가 요청의 audioUrl로 한 번 호출되었다.
# 실패 조건: Modal 결과 파싱, 겹침 시간 계산, 대표 화자 선택, 응답 alias(camelCase)
#       변환 중 하나라도 깨지면 실패해야 한다.
def test_diarization_route_attributes_speakers_from_modal_intervals(monkeypatch):
    calls = []

    class FakeRemote:
        async def aio(self, audio_url):
            calls.append(audio_url)
            return {
                "status": "ok",
                "intervals": [
                    {"speaker_id": 0, "start_ms": 0, "end_ms": 2000},
                    {"speaker_id": 1, "start_ms": 1500, "end_ms": 5000},
                ],
            }

    class FakeFunction:
        remote = FakeRemote()

    monkeypatch.setattr(
        router_module.modal.Function, "from_name", lambda app_name, name: FakeFunction()
    )
    app = create_app("analysis", Settings(_env_file=None))
    app.include_router(diarization_router)
    audio_url = "https://bucket.s3.amazonaws.com/meeting.m4a?X-Amz-Signature=secret"
    segments = [
        {"segmentId": 1, "content": "배포 일정을 정하죠.", "startedAtMs": 0, "endedAtMs": 1800},
        {"segmentId": 2, "content": "금요일이 좋겠습니다.", "startedAtMs": 2000, "endedAtMs": 5000},
    ]

    with TestClient(app) as client:
        response = client.post(
            "/v1/diarization",
            json={
                "requestId": "req-1",
                "meetingId": 42,
                "audioUrl": audio_url,
                "segments": segments,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "meetingId": 42,
        "segments": [
            {**segments[0], "speakerId": 0},
            {**segments[1], "speakerId": 1},
        ],
    }
    assert calls == [audio_url]
