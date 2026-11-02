from fastapi.testclient import TestClient

import meety_ai.speaker_embedding.router as router_module
from meety_ai.app import create_app
from meety_ai.settings import Settings
from meety_ai.speaker_embedding.router import speaker_embedding_router


# 테스트 이름: test_speaker_embedding_route_returns_model_version_and_vector
# 목적: Backend가 사전 녹음 presigned URL을 보내면 저장 가능한 modelVersion과
#       벡터를 받는 계약을 보호한다.
# 준비: create_app("analysis", Settings(_env_file=None))에 speaker_embedding_router를 붙이고,
#       router 모듈이 쓰는 modal.Function.from_name을 성공 결과를 돌려주는 가짜로 바꾼다.
# 실행: POST /v1/speaker-embedding {"audioUrl": "https://...?X-Amz-Signature=secret"}
# 기대 결과: 200, modelVersion과 embedding이 Modal 결과와 같고,
#       가짜 Modal 함수가 요청의 audioUrl로 한 번 호출되었다.
# 실패 조건: 필드명(camelCase)·벡터 전달·버전 전달 중 하나라도 깨지면 실패한다.
def test_speaker_embedding_route_returns_model_version_and_vector(monkeypatch):
    calls = []
    embedding = [0.1 * (i % 7) - 0.3 for i in range(192)]

    class FakeRemote:
        async def aio(self, audio_url):
            calls.append(audio_url)
            return {
                "status": "ok",
                "model_version": "redimnet2-plus-fd45d19-v1",
                "embedding": embedding,
            }

    class FakeFunction:
        remote = FakeRemote()

    monkeypatch.setattr(
        router_module.modal.Function, "from_name", lambda app_name, name: FakeFunction()
    )
    app = create_app("analysis", Settings(_env_file=None))
    app.include_router(speaker_embedding_router)
    audio_url = "https://bucket.s3.amazonaws.com/voice.m4a?X-Amz-Signature=secret"

    with TestClient(app) as client:
        response = client.post("/v1/speaker-embedding", json={"audioUrl": audio_url})

    assert response.status_code == 200
    assert response.json() == {
        "modelVersion": "redimnet2-plus-fd45d19-v1",
        "embedding": embedding,
    }
    assert calls == [audio_url]
