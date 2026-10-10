from typing import Annotated, Literal

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import StringConstraints, ValidationError

from meety_ai.chat.schemas import (
    BackendResponse,
    InternalSummaryResponse,
    MeetingListResponse,
    TranscriptResponse,
)
from meety_ai.core.schemas import MeetingId

QNA_API_PREFIX = "/internal/v1/qna"


class ChatRetrievalError(Exception):
    def __init__(self, code: Literal["forbidden", "retrieval_failed", "timeout"]):
        self.code = code
        super().__init__(
            {
                "forbidden": "회의 자료를 조회하거나 공유할 권한이 없습니다.",
                "retrieval_failed": "회의 자료 조회에 실패했습니다.",
                "timeout": "회의 자료 조회 시간이 초과됐습니다.",
            }[code]
        )


def create_qna_tools(
    client: httpx.AsyncClient, *, team_id: int, ai_request_id: int, current_meeting_id: int
) -> list[BaseTool]:

    async def get_data(path: str, params: dict) -> dict:
        params = {key: value for key, value in params.items() if value is not None}
        try:
            url = QNA_API_PREFIX + path
            response = await client.get(
                url, params=params, headers={"X-Ai-Request-Id": str(ai_request_id)}
            )
            if response.status_code in {401, 403}:
                raise ChatRetrievalError("forbidden")
            envelope = BackendResponse.model_validate(response.json())
            if not envelope.success:
                code = (envelope.error or {}).get("code")
                if response.status_code == 400 and code in {
                    "INVALID_INPUT_VALUE",
                    "INVALID_DATE_RANGE",
                    "INVALID_CURSOR",
                    "INVALID_TRANSCRIPT_KEYWORD",
                }:
                    raise ToolException("검색 조건이 올바르지 않습니다. 조건을 수정해 주세요.")
                if response.status_code == 404 and code in {
                    "TEAM_NOT_FOUND",
                    "MEETING_NOT_FOUND",
                    "SUMMARY_NOT_FOUND",
                }:
                    raise ToolException("대상 회의 자료가 없습니다.")
            if not response.is_success or not envelope.success:
                raise ChatRetrievalError("retrieval_failed")
            return envelope.data
        except httpx.TimeoutException as error:
            raise ChatRetrievalError("timeout") from error
        except (httpx.HTTPError, ValueError) as error:
            raise ChatRetrievalError("retrieval_failed") from error

    @tool
    async def search_meetings(
        keyword: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """팀의 과거 완료 회의를 검색한다. 날짜와 키워드로 조회 범위를 좁힌다."""
        data = await get_data(
            f"/teams/{team_id}/meetings",
            {"keyword": keyword, "from": from_date, "to": to_date, "cursor": cursor},
        )
        try:
            result = MeetingListResponse.model_validate(data)
        except ValidationError as error:
            raise ChatRetrievalError("retrieval_failed") from error

        groups = []
        for group in result.groups:
            meetings = [
                meeting
                for meeting in group.meetings
                if meeting.status == "COMPLETED" and meeting.meeting_id != current_meeting_id
            ]
            if meetings:
                group.meetings = meetings
                group.meeting_count = len(meetings)
                groups.append(group)
        result.groups = groups
        return result.model_dump(by_alias=True)

    @tool
    async def get_meeting_transcript(
        meeting_id: MeetingId,
        keyword: Annotated[str, StringConstraints(min_length=2, max_length=20)] | None = None,
    ) -> dict:
        """지정한 회의의 전사를 조회한다. 키워드가 있으면 관련 구간만 조회한다."""
        data = await get_data(f"/meetings/{meeting_id}/transcripts", {"keyword": keyword})
        try:
            result = TranscriptResponse.model_validate(data)
        except ValidationError as error:
            raise ChatRetrievalError("retrieval_failed") from error
        return {"meetingId": meeting_id, "segments": result.model_dump(by_alias=True)["segments"]}

    @tool
    async def get_meeting_summary(meeting_id: MeetingId) -> dict:
        """회의 최신 요약을 조회한다. 사용 가능한 요약이 없으면 전사를 조회한다."""
        data = await get_data(f"/meetings/{meeting_id}/summaries", {})
        try:
            result = InternalSummaryResponse.model_validate(data).summary
        except ValidationError as error:
            raise ChatRetrievalError("retrieval_failed") from error
        if result is None:
            return {"meetingId": meeting_id, "status": "NOT_FOUND", "content": None}
        if result.status != "COMPLETED":
            return {"meetingId": meeting_id, "status": result.status, "content": None}
        return {
            "meetingId": meeting_id,
            "summaryId": result.summary_id,
            "status": result.status,
            "content": result.content,
        }

    tools = [search_meetings, get_meeting_transcript, get_meeting_summary]
    for retrieval_tool in tools:
        retrieval_tool.handle_tool_error = True
        retrieval_tool.handle_validation_error = "조회 인자 형식이 올바르지 않습니다."
    return tools
