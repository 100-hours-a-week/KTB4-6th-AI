"""전역 AI 채팅의 조회·초안 도구와 요청별 실행 기록을 제공한다."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.config import get_stream_writer
from pydantic import ValidationError

from meety_ai.assistant.schemas import MeetingDraft, MeetingMetricsResponse, TeamMembersResponse
from meety_ai.chat.schemas import InternalSummaryResponse, MeetingListResponse, TranscriptResponse
from meety_ai.chat.tools import ChatRetrievalError, get_data
from meety_ai.core.schemas import MeetingId

ASSISTANT_API_PREFIX = "/internal/v1/assistant"
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """시간대 없는 KST 현재 시각을 반환한다."""
    return datetime.now(KST).replace(tzinfo=None, microsecond=0)


@dataclass
class AssistantRun:
    """한 요청에서 도구와 미들웨어가 공유하는 실행 기록."""

    max_meetings: int
    max_input_bytes: int
    input_bytes: int = 0
    targets: set[int] = field(default_factory=set)
    # 삽입 순서를 유지해 조회한 순서대로 반환한다.
    referenced_meeting_ids: dict[int, None] = field(default_factory=dict)
    meeting_draft: MeetingDraft | None = None


def _format_validation_error(error: ValidationError) -> str:
    details = "; ".join(entry["msg"] for entry in error.errors(include_input=False))
    return f"입력값을 고쳐 다시 호출하세요: {details}"


def create_assistant_tools(
    client: httpx.AsyncClient, *, team_id: int, ai_request_id: int, run: AssistantRun
) -> list[BaseTool]:
    headers = {"X-Ai-Request-Id": str(ai_request_id)}

    def admit(meeting_id: int) -> None:
        if meeting_id not in run.targets and len(run.targets) >= run.max_meetings:
            raise ToolException(
                f"분석 대상은 회의 {run.max_meetings}개까지입니다. "
                "확인한 자료로 답하고 나머지는 보지 못했다고 밝히세요."
            )
        run.targets.add(meeting_id)

    def accept(payload: dict, meeting_id: int, *, has_content: bool) -> dict:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if run.input_bytes + size > run.max_input_bytes:
            raise ToolException(
                "자료가 입력 크기 제한을 넘어 제외했습니다. 요약이나 이미 확인한 자료로 "
                "답하고, 이 자료를 보지 못했다고 밝히세요."
            )
        run.input_bytes += size
        if has_content:
            run.referenced_meeting_ids[meeting_id] = None
        return payload

    async def read_meeting(path: str, response_model, meeting_id: int):
        get_stream_writer()({"phase": "READING_MEETING"})
        admit(meeting_id)
        try:
            return await get_data(client, path, {}, response_model, headers)
        except ChatRetrievalError as error:
            # 인증 실패는 요청 전체가 무효이지만 회의 하나의 장애는 나머지 자료로 답할 수 있다.
            if error.code == "forbidden":
                raise
            raise ToolException(
                f"회의 {meeting_id}의 자료를 가져오지 못했습니다. "
                "자료 없음으로 해석하지 말고 답변에 조회 실패를 밝히세요."
            ) from error

    @tool
    async def search_meetings(
        keyword: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """팀의 모든 상태 회의를 날짜 그룹으로 검색한다. 날짜와 키워드로 범위를 좁힌다."""
        get_stream_writer()({"phase": "SEARCHING_MEETINGS"})
        result = await get_data(
            client,
            f"/teams/{team_id}/meetings",
            {"keyword": keyword, "from": from_date, "to": to_date, "cursor": cursor},
            MeetingListResponse,
            headers,
        )
        payload = result.model_dump(by_alias=True)
        run.input_bytes += len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return payload

    @tool
    async def get_meeting_transcript(meeting_id: MeetingId) -> dict:
        """진행 중 회의를 포함한 회의의 전사를 조회한다. 요약으로 부족할 때만 사용한다."""
        result = await read_meeting(
            f"/meetings/{meeting_id}/transcripts", TranscriptResponse, meeting_id
        )
        payload = {
            "meetingId": meeting_id,
            "segments": result.model_dump(by_alias=True)["segments"],
        }
        return accept(payload, meeting_id, has_content=bool(payload["segments"]))

    @tool
    async def get_meeting_summary(meeting_id: MeetingId) -> dict:
        """회의의 최신 요약을 조회한다."""
        response = await read_meeting(
            f"/meetings/{meeting_id}/summaries", InternalSummaryResponse, meeting_id
        )
        summary = response.summary
        if summary is None:
            return {"meetingId": meeting_id, "status": "NOT_FOUND", "content": None}
        if summary.status != "COMPLETED":
            return {"meetingId": meeting_id, "status": summary.status, "content": None}
        payload = {"meetingId": meeting_id, "status": summary.status, "content": summary.content}
        return accept(payload, meeting_id, has_content=bool(summary.content))

    @tool
    async def get_meeting_metrics(meeting_id: MeetingId) -> dict:
        """회의의 지표를 조회한다. null은 계산 불가이며 0과 다르다."""
        result = await read_meeting(
            f"/meetings/{meeting_id}/metrics", MeetingMetricsResponse, meeting_id
        )
        payload = {"meetingId": meeting_id, **result.model_dump(by_alias=True)}
        has_content = any(
            value is not None
            for value in (
                result.metrics,
                result.speech_balance_score,
                result.context_out_duration_ms,
            )
        )
        return accept(payload, meeting_id, has_content=has_content)

    async def fetch_team_members() -> TeamMembersResponse:
        return await get_data(
            client,
            "/team-members",
            {},
            TeamMembersResponse,
            headers,
            prefix=ASSISTANT_API_PREFIX,
        )

    @tool
    async def get_team_members() -> dict:
        """활성 팀원의 teamMemberId와 이름을 조회한다."""
        get_stream_writer()({"phase": "PREPARING_MEETING"})
        return (await fetch_team_members()).model_dump(by_alias=True)

    @tool
    async def propose_meeting_draft(draft: MeetingDraft) -> str:
        """필수 값이 모두 정해졌을 때 회의 초안을 등록한다. 회의를 생성하지는 않는다."""
        get_stream_writer()({"phase": "PREPARING_MEETING"})
        if datetime.fromisoformat(draft.scheduled_at) <= now_kst():
            raise ToolException("예정 시각이 이미 지났습니다. 사용자에게 다시 확인하세요.")
        members = await fetch_team_members()
        active_ids = {member.team_member_id for member in members.members}
        if not set(draft.attendee_team_member_ids) <= active_ids:
            raise ToolException("활성 팀원이 아닌 참석자 ID가 있습니다. 팀원 목록을 확인하세요.")
        run.meeting_draft = draft
        return "초안을 등록했습니다. 이제 같은 내용을 Markdown으로 설명하고 확인을 요청하세요."

    tools = [
        search_meetings,
        get_meeting_transcript,
        get_meeting_summary,
        get_meeting_metrics,
        get_team_members,
        propose_meeting_draft,
    ]
    for assistant_tool in tools:
        assistant_tool.handle_tool_error = True
        assistant_tool.handle_validation_error = _format_validation_error
    return tools
