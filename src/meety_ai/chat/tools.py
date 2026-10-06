"""질문별 고정 맥락으로 Backend 회의 자료를 조회한다."""

import json
from typing import Annotated, Literal

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import StringConstraints

from meety_ai.chat.schemas import Identifier, TranscriptionSegment

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
QNA_API_PREFIX = "/internal/v1/qna"


class QnARetrievalError(Exception):
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
    client: httpx.AsyncClient,
    *,
    team_id: int,
    ai_request_id: str,
    current_meeting_id: str,
) -> list[BaseTool]:
    async def get_data(path: str, **params) -> dict:
        params = {key: value for key, value in params.items() if value is not None}
        params["aiRequestId"] = ai_request_id
        try:
            url = f"{QNA_API_PREFIX}{path}"
            async with client.stream("GET", url, params=params) as response:
                if response.status_code == 403:
                    raise QnARetrievalError("forbidden")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise QnARetrievalError("retrieval_failed")
                envelope = json.loads(body)
                if not isinstance(envelope, dict):
                    raise QnARetrievalError("retrieval_failed")
                success = envelope.get("success")
                data, error = envelope.get("data"), envelope.get("error")
                if success is False and data is None and isinstance(error, dict):
                    code = error.get("code")
                    if code in {
                        "TEAM_MEMBERSHIP_REQUIRED",
                        "MEETING_ACCESS_DENIED",
                        "NO_ACTIVE_TEAM",
                        "TEAM_ACCESS_DENIED",
                    }:
                        raise QnARetrievalError("forbidden")
                    if response.status_code in (200, 400) and code in {
                        "INVALID_MEETING_KEYWORD",
                        "INVALID_DATE_RANGE",
                        "INVALID_CURSOR",
                        "INVALID_TRANSCRIPT_KEYWORD",
                        "INVALID_SUMMARY_VERSION",
                    }:
                        raise ToolException("검색 조건이 올바르지 않습니다. 조건을 수정해 주세요.")
                    if response.status_code in (200, 404) and code in {
                        "TEAM_NOT_FOUND",
                        "MEETING_NOT_FOUND",
                        "SUMMARY_NOT_FOUND",
                    }:
                        raise ToolException("대상 회의 자료가 없습니다.")
                if not response.is_success or success is not True or error is not None:
                    raise QnARetrievalError("retrieval_failed")
                if "error" not in envelope or not isinstance(data, dict):
                    raise QnARetrievalError("retrieval_failed")
                return data
        except httpx.TimeoutException as error:
            raise QnARetrievalError("timeout") from error
        except (httpx.HTTPError, ValueError) as error:
            raise QnARetrievalError("retrieval_failed") from error

    @tool
    async def search_meetings(
        keyword: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """과거 완료 회의를 제목으로 검색한다. 날짜는 YYYY-MM-DD, cursor는 이전 결과를 쓴다.

        전사 내용 검색이 아니다. 빈 목록이어도 hasNext가 true이면 다음 페이지가 있다.
        """
        data = await get_data(
            f"/teams/{team_id}/meetings",
            **{"keyword": keyword, "from": from_date, "to": to_date, "cursor": cursor},
        )
        try:
            if not isinstance(data["groups"], list) or type(data["hasNext"]) is not bool:
                raise ValueError
            cursor_value = data["nextCursor"]
            if cursor_value is not None and not isinstance(cursor_value, str):
                raise ValueError
            groups = []
            for group in data["groups"]:
                if not isinstance(group["meetings"], list):
                    raise ValueError
                meetings = []
                for meeting in group["meetings"]:
                    meeting_id = meeting["meetingId"]
                    if type(meeting_id) is not int or meeting_id <= 0:
                        raise ValueError
                    if meeting["status"] not in {"WAITING", "IN_PROGRESS", "COMPLETED"}:
                        raise ValueError
                    if meeting["status"] == "COMPLETED" and str(meeting_id) != current_meeting_id:
                        meetings.append({**meeting, "meetingId": str(meeting_id)})
                if meetings:
                    groups.append({**group, "meetings": meetings, "meetingCount": len(meetings)})
            return {"groups": groups, "nextCursor": cursor_value, "hasNext": data["hasNext"]}
        except (KeyError, TypeError, ValueError) as error:
            raise QnARetrievalError("retrieval_failed") from error

    @tool
    async def get_meeting_transcript(
        meeting_id: Identifier,
        keyword: Annotated[str, StringConstraints(min_length=2, max_length=20)] | None = None,
    ) -> dict:
        """회의 전체 확정 전사를 조회한다. keyword를 지정하면 2~20자로 발언을 검색한다."""
        data = await get_data(f"/meetings/{meeting_id}/transcripts", keyword=keyword)
        try:
            if not isinstance(data["segments"], list):
                raise ValueError
            segments = []
            for segment in data["segments"]:
                parsed = TranscriptionSegment.model_validate(
                    {key: value for key, value in segment.items() if key != "recognizedAt"}
                )
                if parsed.started_at_ms > parsed.ended_at_ms:
                    raise ValueError
                segments.append(parsed.model_dump(by_alias=True))
            return {"meetingId": meeting_id, "segments": segments}
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise QnARetrievalError("retrieval_failed") from error

    @tool
    async def get_meeting_summary(meeting_id: Identifier) -> dict:
        """회의 최신 요약을 조회한다. 사용 가능한 요약이 없으면 전사를 조회한다."""
        data = await get_data(f"/meetings/{meeting_id}/summaries")
        summary_id, status, content = data.get("summaryId"), data.get("status"), data.get("content")
        if type(summary_id) is not int or summary_id <= 0 or not isinstance(status, str):
            raise QnARetrievalError("retrieval_failed")
        if status != "COMPLETED":
            if content is not None:
                raise QnARetrievalError("retrieval_failed")
            return {"meetingId": meeting_id, "status": status, "content": None}
        if not isinstance(content, str) or not content.strip():
            raise QnARetrievalError("retrieval_failed")
        return {
            "meetingId": meeting_id,
            "summaryId": summary_id,
            "status": status,
            "content": content,
        }

    tools = [search_meetings, get_meeting_transcript, get_meeting_summary]
    for retrieval_tool in tools:
        retrieval_tool.handle_tool_error = True
        retrieval_tool.handle_validation_error = "조회 인자 형식이 올바르지 않습니다."
    return tools
