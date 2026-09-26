import json

import pytest
from pydantic import ValidationError

from meety_ai.summary.schemas import SummaryRequest

# 테스트 이름: test_summary_request_accepts_complete_transcript
# 목적: Backend가 보낸 종료된 회의의 전체 전사 요청이 요약 처리에 필요한 값을 보존하는지 확인한다.
# 준비: 회의 정보, 화자 목록, 순서가 있는 전사 구간 목록을
# Backend 요청 형식인 camelCase로 준비한다.
# 실행: 요청 JSON을 SummaryRequest로 검증한다.
# 기대 결과: 요청이 통과하고 회의 시각, 화자 참조, 전사 내용과 시간 정보가 손실 없이 보존된다.
# 실패 조건: 유효한 요청을 거부하거나 입력 필드를 누락·변경하면 실패해야 한다.

def test_summary_request_accepts_complete_transcript():
    payload = {
        "requestId": "req-1",
        "meetingId": 42,
        "title": "주간 회의",
        "purpose": "진행 상황 공유",
        "note": "다음 일정 확인",
        "meetingStartedAt": "2026-09-22T10:00:00+09:00",
        "speakers": [
            {"speakerId": 1, "teamMemberId": 7, "displayName": "홍길동"},
        ],
        "segments": [
            {
                "segmentId": 1,
                "speakerId": 1,
                "sequenceNumber": 0,
                "content": "진행 상황을 공유하겠습니다.",
                "startedAtMs": 0,
                "endedAtMs": 1800,
            },
        ],
    }

    request = SummaryRequest.model_validate_json(json.dumps(payload))

    assert request.model_dump(by_alias=True, mode="json") == payload


def test_empty_transcript_segment_request():
    payload = {
        "requestId": "req-1",
        "meetingId": 42,
        "title": "주간 회의",
        "purpose": "진행 상황 공유",
        "note": "다음 일정 확인",
        "meetingStartedAt": "2026-09-22T10:00:00+09:00",
        "speakers": [
            {"speakerId": 1, "teamMemberId": 7, "displayName": "홍길동"},
        ],
        "segments": [],
    }

    with pytest.raises(ValidationError) as error:
        SummaryRequest.model_validate_json(json.dumps(payload))

    assert error.value.errors()[0]["loc"] == ("segments",)


def test_summary_regeneration_request():
    payload = {
            "requestId": "req-1",
            "meetingId": 42,
            "title": "주간 회의",
            "purpose": "진행 상황 공유",
            "note": "다음 일정 확인",
            "meetingStartedAt": "2026-09-22T10:00:00+09:00",
            "speakers": [
                {"speakerId": 1, "teamMemberId": 7, "displayName": "홍길동"},
            ],
            "segments": [
                {
                    "segmentId": 1,
                    "speakerId": 1,
                    "sequenceNumber": 0,
                    "content": "진행 상황을 공유하겠습니다. 오늘은 요약 기능 스키마를 구현했습니다.",
                    "startedAtMs": 0,
                    "endedAtMs": 1800,
                },
            ],
            "previousSummary": "요약 기능을 전부 구현함",
            "regenerationReason": "전부가 아닌 스키마만 구현함"
        }
    
    request = SummaryRequest.model_validate_json(json.dumps(payload))

    assert request.model_dump(by_alias=True, mode="json") == payload