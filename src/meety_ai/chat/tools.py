from typing import Literal

import httpx
import structlog
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import ValidationError

from meety_ai.chat.schemas import (
    BackendResponse,
    InternalSummaryResponse,
    MeetingListResponse,
    TranscriptResponse,
)
from meety_ai.core.schemas import MeetingId, Message

logger = structlog.stdlib.get_logger(__name__)

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

    async def get_data[T: Message](path: str, params: dict, response_model: type[T]) -> T:
        params = {key: value for key, value in params.items() if value is not None}
        url = QNA_API_PREFIX + path
        log = logger.bind(path=url, ai_request_id=ai_request_id)
        for attempt in (1, 2):
            try:
                response = await client.get(
                    url, params=params, headers={"X-Ai-Request-Id": str(ai_request_id)}
                )
            except httpx.HTTPError as error:
                retry = attempt == 1 and isinstance(error, httpx.TransportError)
                log.warning(
                    "chat_retrieval_transport_failed",
                    attempt=attempt,
                    retry=retry,
                    error_type=type(error).__name__,
                )
                if retry:
                    continue
                code = (
                    "timeout" if isinstance(error, httpx.TimeoutException) else "retrieval_failed"
                )
                raise ChatRetrievalError(code) from error
            if response.is_server_error and attempt == 1:
                log.warning(
                    "chat_retrieval_retry", status_code=response.status_code, attempt=attempt
                )
                continue
            break

        log = log.bind(status_code=response.status_code)
        try:
            envelope = BackendResponse.model_validate(response.json())
            code = (envelope.error or {}).get("code")
            if not response.is_success or not envelope.success:
                log.warning("chat_retrieval_rejected", backend_code=code)
                if not envelope.success:
                    if response.status_code == 403 and code == "MEETING_ACCESS_DENIED":
                        raise ToolException(
                            "이 회의는 조회할 수 없습니다. "
                            "회의 검색으로 조회 가능한 회의 ID를 확인하세요."
                        )
                    if response.status_code == 400 and code in {
                        "INVALID_INPUT_VALUE",
                        "INVALID_DATE_RANGE",
                        "INVALID_CURSOR",
                        "INVALID_TRANSCRIPT_KEYWORD",
                        "INVALID_MEETING_KEYWORD",
                    }:
                        raise ToolException("검색 조건이 올바르지 않습니다. 조건을 수정해 주세요.")
                    if response.status_code == 404 and code in {
                        "TEAM_NOT_FOUND",
                        "MEETING_NOT_FOUND",
                        "SUMMARY_NOT_FOUND",
                    }:
                        raise ToolException(
                            "대상 회의 자료가 없습니다. 검색 조건이나 회의 ID를 확인하세요."
                        )
                if response.status_code in {401, 403}:
                    raise ChatRetrievalError("forbidden")
                raise ChatRetrievalError("retrieval_failed")
            return response_model.model_validate(envelope.data)
        except ValueError as error:
            log.warning(
                "chat_retrieval_invalid_response",
                error_type=type(error).__name__,
                validation_errors=[
                    {"field": entry["loc"], "type": entry["type"]}
                    for entry in error.errors(include_input=False)
                ]
                if isinstance(error, ValidationError)
                else [],
            )
            code = "forbidden" if response.status_code in {401, 403} else "retrieval_failed"
            raise ChatRetrievalError(code) from error

    @tool
    async def search_meetings(
        keyword: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """팀의 과거 완료 회의를 검색한다. 날짜와 키워드로 조회 범위를 좁힌다."""
        result = await get_data(
            f"/teams/{team_id}/meetings",
            {"keyword": keyword, "from": from_date, "to": to_date, "cursor": cursor},
            MeetingListResponse,
        )

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
        # keyword: Annotated[str, StringConstraints(min_length=2, max_length=20)] | None = None,
    ) -> dict:
        """과거 회의의 전사를 조회한다. 현재 회의는 요청에 제공된 전사를 사용한다."""
        if meeting_id == current_meeting_id:
            raise ToolException("현재 회의는 조회하지 말고 요청에 제공된 전사를 사용하세요.")
        result = await get_data(f"/meetings/{meeting_id}/transcripts", {}, TranscriptResponse)
        return {"meetingId": meeting_id, "segments": result.model_dump(by_alias=True)["segments"]}

    @tool
    async def get_meeting_summary(meeting_id: MeetingId) -> dict:
        """과거 회의의 최신 요약을 조회한다. 요약이 없으면 해당 회의의 전사를 조회한다."""
        if meeting_id == current_meeting_id:
            raise ToolException("현재 회의는 조회하지 말고 요청에 제공된 전사를 사용하세요.")
        response = await get_data(f"/meetings/{meeting_id}/summaries", {}, InternalSummaryResponse)
        result = response.summary
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
