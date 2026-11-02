import asyncio
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from meety_ai.app import create_app
from meety_ai.metric.chain import create_metric_chain
from meety_ai.metric.router import WORK_TIMEOUT_SECONDS as METRIC_TIMEOUT_SECONDS
from meety_ai.metric.router import metric_router
from meety_ai.settings import Settings
from meety_ai.summary.chain import create_summary_chain
from meety_ai.summary.router import WORK_TIMEOUT_SECONDS as SUMMARY_TIMEOUT_SECONDS
from meety_ai.summary.router import summary_router


def analysis_payload():
    return {
        "requestId": "issue-119",
        "meetingId": 175,
        "summary": "### 배포\n배포 일정 논의",
        "segments": [
            {
                "segmentId": segment_id,
                "speakerId": None,
                "sequenceNumber": sequence,
                "content": content,
                "startedAtMs": start,
                "endedAtMs": end,
            }
            for segment_id, sequence, content, start, end in [
                (20, 9, "학교 이야기", 2000, 3000),
                (30, 3, "배포 점검", 500, 1500),
                (10, 1, "배포 일정", 0, 1000),
            ]
        ],
    }


# 테스트 이름: test_metric_provider_round_trip
# 목적: 실제 체인의 공급자 요청부터 인덱스 검증·시간 계산·HTTP 응답까지 보호한다.
# 준비: HTTP 공급자만 대체하고, DB ID와 정렬 순서가 다른 전사를 전달한다.
# 실행: 유효하거나 잘못된 모델 범위를 반환한 뒤 /v1/metric을 호출한다.
# 기대 결과: 제한된 출력 스키마·인덱스 전사를 전달하며 유효 범위만 200으로 계산한다.
# 실패 조건: 정렬·매핑 오류, 중복 합산, 잘못된 범위를 보정하거나 버려 성공 처리한다.
@pytest.mark.parametrize(
    "start,end,status", [(0, 1, 200), (0, 3, 502), (2, 1, 502), (-1, 1, 502), (0, 1.5, 502)]
)
async def test_metric_provider_round_trip(start, end, status):
    async def provider(request):
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["reasoning"] == {"effort": "xhigh"}
        output = body["response_format"]["json_schema"]
        assert output["strict"] is True
        for name in ("startIndex", "endIndex"):
            field = output["schema"]["$defs"]["SegmentRange"]["properties"][name]
            assert field["minimum"] == 0
            assert field["maximum"] == 2
        transcript = json.loads(body["messages"][1]["content"].split("전사:\n")[1])
        assert [s["index"] for s in transcript] == [0, 1, 2]
        assert [s["content"] for s in transcript] == ["배포 일정", "배포 점검", "학교 이야기"]
        assert all("segmentId" not in s for s in transcript)
        classification = {
            "agendas": [
                {
                    "title": "배포",
                    "ranges": [
                        {"startIndex": start, "endIndex": end},
                        {"startIndex": 0, "endIndex": 0},
                    ],
                }
            ],
            "offTopic": [{"reason": "학교 이야기", "ranges": [{"startIndex": 2, "endIndex": 2}]}],
        }
        return httpx.Response(
            200,
            json={
                "id": "test",
                "model": "test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(classification)},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        model = ChatOpenAI(
            model="openai/gpt-6-luna",
            api_key="test",
            http_async_client=provider_client,
            max_retries=0,
        )
        app = create_app("analysis", Settings(_env_file=None))
        app.state.metric_chain = create_metric_chain(model)
        app.include_router(metric_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/metric", json=analysis_payload())
    assert response.status_code == status
    if status == 200:
        assert response.json() == {
            "requestId": "issue-119",
            "meetingId": 175,
            "agendas": [{"title": "배포", "durationMs": 1500}],
            "offTopic": {"durationMs": 1000},
        }


# 테스트 이름: test_analysis_deadline_cancels_provider
# 목적: 모델 클라이언트 timeout과 별개인 작업 기한이 공급자 대기를 취소하는지 확인한다.
# 준비: 공급자 응답을 지연시키고 라우터의 작업 기한만 줄인다.
# 실행: 요약·지표 HTTP 요청을 보낸다.
# 기대 결과: 공급자 대기가 취소되고 최종 504 응답을 반환한다.
# 실패 조건: 공급자 timeout까지 계속 기다리거나 500 또는 성공으로 반환한다.
@pytest.mark.parametrize("route", ["summary", "metric"])
async def test_analysis_deadline_cancels_provider(route, monkeypatch):
    assert METRIC_TIMEOUT_SECONDS == SUMMARY_TIMEOUT_SECONDS == 180
    assert Settings(_env_file=None).summary_timeout_seconds == 180
    cancelled = asyncio.Event()

    async def provider(request):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(f"meety_ai.{route}.router.WORK_TIMEOUT_SECONDS", 0.05)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        model = ChatOpenAI(
            model="openai/gpt-6-luna",
            api_key="test",
            http_async_client=provider_client,
            max_retries=0,
            timeout=180,
        )
        app = create_app("analysis", Settings(_env_file=None))
        app.state.summary_chain = create_summary_chain(model)
        app.state.metric_chain = create_metric_chain(model)
        app.include_router(summary_router)
        app.include_router(metric_router)
        payload = analysis_payload()
        if route == "summary":
            payload.pop("summary")
            payload.update(
                title="회의",
                purpose="배포",
                note="",
                meetingStartedAt="2026-10-27T10:00:00+09:00",
                speakers=[],
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with asyncio.timeout(2):
                response = await client.post(f"/v1/{route}", json=payload)
    assert response.status_code == 504
    assert cancelled.is_set()
