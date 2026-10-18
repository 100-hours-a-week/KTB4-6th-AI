import httpx
import pytest

from meety_ai.chat.tools import create_qna_tools


@pytest.mark.parametrize(
    ("tool_index", "arguments", "expected_path", "data"),
    [
        (
            0,
            {},
            "/internal/v1/qna/teams/3/meetings",
            {"groups": [], "nextCursor": None, "hasNext": False},
        ),
        (
            1,
            {"meeting_id": 7},
            "/internal/v1/qna/meetings/7/transcripts",
            {
                "segments": [
                    {
                        "segmentId": 1,
                        "speakerDisplayName": None,
                        "sequenceNumber": 0,
                        "content": "배포 일정 논의",
                        "startedAtMs": 0,
                        "endedAtMs": None,
                    }
                ]
            },
        ),
        (
            2,
            {"meeting_id": 7},
            "/internal/v1/qna/meetings/7/summaries",
            {
                "summary": {
                    "summaryId": 1,
                    "status": "COMPLETED",
                    "content": "요약",
                    "version": 1,
                    "failureReason": None,
                    "createdAt": "2026-10-10T10:00:00",
                }
            },
        ),
        (2, {"meeting_id": 7}, "/internal/v1/qna/meetings/7/summaries", {"summary": None}),
    ],
)
async def test_retrieval_tools_preserve_qna_prefix(tool_index, arguments, expected_path, data):
    def handle(request):
        assert request.url.path == expected_path
        assert request.headers["X-Ai-Request-Id"] == "88"
        assert request.headers["X-Internal-Api-Key"] == "test-internal-key"
        assert "aiRequestId" not in request.url.params
        return httpx.Response(200, json={"success": True, "data": data, "error": None})

    async with httpx.AsyncClient(
        base_url="https://backend.test",
        headers={"X-Internal-Api-Key": "test-internal-key"},
        transport=httpx.MockTransport(handle),
    ) as client:
        tools = create_qna_tools(client, team_id=3, ai_request_id=88, current_meeting_id=42)
        result = await tools[tool_index].ainvoke(arguments)
        if tool_index == 1:
            assert result == {"meetingId": 7, "segments": data["segments"]}
        if tool_index == 2:
            if data["summary"] is None:
                assert result == {"meetingId": 7, "status": "NOT_FOUND", "content": None}
            else:
                assert result == {
                    "meetingId": 7,
                    "summaryId": 1,
                    "status": "COMPLETED",
                    "content": "요약",
                }


@pytest.mark.parametrize("tool_index", [1, 2])
async def test_current_meeting_lookup_guides_agent_without_backend_request(tool_index):
    requests = []

    def handle(request):
        requests.append(request)
        data = {"segments": []} if request.url.path.endswith("/transcripts") else {"summary": None}
        return httpx.Response(200, json={"success": True, "data": data, "error": None})

    async with httpx.AsyncClient(
        base_url="https://backend.test", transport=httpx.MockTransport(handle)
    ) as client:
        tools = create_qna_tools(client, team_id=3, ai_request_id=88, current_meeting_id=42)
        result = await tools[tool_index].ainvoke({"meeting_id": 42})

    assert result == "현재 회의는 조회하지 말고 요청에 제공된 전사를 사용하세요."
    assert requests == []
