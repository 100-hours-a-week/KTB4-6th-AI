# 테스트 이름: test_metric_route_returns_overlapping_estimates
# 목적: 요약과 전사로 안건 시간·맥락 이탈 시간만 계산하며 안건·잡담 중복을 허용한다.
# 준비: create_app("analysis", Settings(_env_file=None))에 metric_router를 등록한다.
# 준비: app.state.metric_chain을 고정 MetricClassification을 반환하는 AsyncMock으로 대체한다.
# 준비: 전사 ID 10·30·20의 sequenceNumber는 0·1·2이다.
# 준비: 전사 시각은 0~1000, 500~1500, 2000~3000ms이며 대표 화자는 1·1·null이다.
# 준비: 안건 A 범위는 10~20, 안건 B와 잡담 범위는 30~30으로 중복 지정한다.
# 실행: TestClient로 POST /v1/metric에 requestId·meetingId·summary·segments를 전달한다.
# 기대 결과: 200 JSON이며 총 소요 시간·초과 시간·화자 시간 필드는 없다.
# 기대 결과: 안건 A 2500ms, 안건 B 1000ms, 잡담 1000ms이며 구간 범위는 응답에 포함하지 않는다.
# 기대 결과: LLM 한 번 호출, 기존 요약·화자 분리 응답 계약 유지, TestClient 종료로 자원 정리.
# 실패 조건: 숫자 ID 순으로 범위를 해석하거나 침묵 포함, 항목 내부 중복 합산, 항목 간 중복 제거.

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APITimeoutError

from meety_ai.app import create_app
from meety_ai.metric.router import metric_router
from meety_ai.metric.schemas import MetricClassification
from meety_ai.settings import Settings


@pytest.fixture
def metric_payload():
    return {
        "requestId": "req-metric",
        "meetingId": 42,
        "summary": "### 안건 A\n배포 일정 논의",
        "segments": [
            {
                "segmentId": segment_id,
                "speakerId": speaker_id,
                "sequenceNumber": sequence,
                "content": "회의 발화",
                "startedAtMs": start,
                "endedAtMs": end,
            }
            for sequence, (segment_id, speaker_id, start, end) in enumerate(
                [(10, 1, 0, 1000), (30, 1, 500, 1500), (20, None, 2000, 3000)]
            )
        ],
    }


def test_metric_route_returns_overlapping_estimates(metric_payload):
    classification = MetricClassification.model_validate(
        {
            "agendas": [
                {
                    "title": "안건 A",
                    "ranges": [
                        {"startSegmentId": 10, "endSegmentId": 20},
                        {"startSegmentId": 10, "endSegmentId": 30},
                    ],
                },
                {
                    "title": "안건 B",
                    "ranges": [{"startSegmentId": 30, "endSegmentId": 30}],
                },
            ],
            "offTopic": [
                {
                    "ranges": [{"startSegmentId": 30, "endSegmentId": 30}],
                    "reason": "개인적인 잡담",
                }
            ],
        }
    )
    invoke = AsyncMock(return_value=classification)
    app = create_app("analysis", Settings(_env_file=None))
    app.state.metric_chain = SimpleNamespace(ainvoke=invoke)
    app.include_router(metric_router)

    with TestClient(app) as client:
        response = client.post("/v1/metric", json=metric_payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    result = response.json()
    assert set(result) == {
        "requestId",
        "meetingId",
        "offTopic",
        "agendas",
    }
    assert result["requestId"] == "req-metric"
    assert result["meetingId"] == 42
    assert result["agendas"] == [
        {"title": "안건 A", "durationMs": 2500},
        {"title": "안건 B", "durationMs": 1000},
    ]
    assert result["offTopic"] == {"durationMs": 1000}
    invoke.assert_awaited_once()


def test_metric_route_keeps_off_topic_duration_when_segments_are_missing(metric_payload):
    classification = MetricClassification.model_validate(
        {
            "agendas": [
                {
                    "title": "안건 A",
                    "ranges": [{"startSegmentId": 10, "endSegmentId": 30}],
                }
            ],
            "offTopic": [
                {
                    "ranges": [{"startSegmentId": 30, "endSegmentId": 30}],
                    "reason": "개인적인 잡담",
                }
            ],
        }
    )
    app = create_app("analysis", Settings(_env_file=None))
    app.state.metric_chain = SimpleNamespace(ainvoke=AsyncMock(return_value=classification))
    app.include_router(metric_router)

    with TestClient(app) as client:
        response = client.post("/v1/metric", json=metric_payload)

    assert response.status_code == 200
    result = response.json()
    assert result["agendas"][0]["durationMs"] == 1500
    assert result["offTopic"]["durationMs"] == 1000


@pytest.mark.parametrize("failure,status", [("connection", 502), ("timeout", 504), ("range", 502)])
def test_metric_route_returns_error_status_on_generation_failure(metric_payload, failure, status):
    request = httpx.Request("POST", "https://provider.invalid")
    if failure == "connection":
        invoke = AsyncMock(side_effect=APIConnectionError(request=request))
    elif failure == "timeout":
        invoke = AsyncMock(side_effect=APITimeoutError(request=request))
    else:
        invoke = AsyncMock(
            return_value=MetricClassification.model_validate(
                {
                    "agendas": [
                        {
                            "title": "안건 A",
                            "ranges": [{"startSegmentId": 10, "endSegmentId": 999}],
                        }
                    ],
                    "offTopic": [],
                }
            )
        )
    app = create_app("analysis", Settings(_env_file=None))
    app.state.metric_chain = SimpleNamespace(ainvoke=invoke)
    app.include_router(metric_router)

    with TestClient(app) as client:
        response = client.post("/v1/metric", json=metric_payload)

    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    invoke.assert_awaited_once()
