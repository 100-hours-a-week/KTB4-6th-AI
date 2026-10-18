import json

from meety_ai.chat.schemas import ChatRequest, ChatResponse


def test_chat_request_and_response_preserve_flat_contract():
    request_json = {
        "meetingId": 42,
        "teamId": 3,
        "aiRequestId": 88,
        "question": "지난 회의에서 정한 배포 일정은 언제인가요?",
        "conversationHistory": [
            {"role": "user", "content": "지난 회의에서 어떤 결정을 했나요?"},
            {"role": "assistant", "content": "배포 일정을 결정했습니다."},
            {"role": "user", "content": "담당자도 정했나요?"},
        ],
        "transcriptSegments": [
            {
                "segmentId": 0,
                "speakerDisplayName": None,
                "sequenceNumber": 0,
                "content": "지난 회의 배포 일정을 확인하겠습니다.",
                "startedAtMs": 0,
                "endedAtMs": 1800,
            }
        ],
    }

    request = ChatRequest.model_validate_json(json.dumps(request_json))

    assert request.model_dump(mode="json", by_alias=True) == request_json

    response = ChatResponse(
        ai_request_id=request.ai_request_id,
        answer="## 배포 일정\n지난 회의 요약에 따르면 금요일입니다.",
        citations=[{"sourceType": "summary", "meetingId": 7, "summaryId": 1}],
    )

    assert json.loads(response.model_dump_json(by_alias=True, exclude_none=True)) == {
        "aiRequestId": 88,
        "answer": "## 배포 일정\n지난 회의 요약에 따르면 금요일입니다.",
        "citations": [{"sourceType": "summary", "meetingId": 7, "summaryId": 1}],
    }
