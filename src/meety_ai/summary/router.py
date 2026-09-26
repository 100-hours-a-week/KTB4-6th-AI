from fastapi import APIRouter, HTTPException, Request, Response
from openai import APIError, APITimeoutError

from meety_ai.summary.schemas import SummaryRequest

summary_router = APIRouter()


@summary_router.post("/v1/summary")
async def generate_summary(payload: SummaryRequest, request: Request) -> Response:
    speaker_names = {
        speaker.speaker_id: speaker.display_name or f"화자 {speaker.speaker_id}"
        for speaker in payload.speakers
    }
    speaker_names[None] = "미확인 화자"
    transcript = "\n".join(
        f"{speaker_names.get(segment.speaker_id, f'화자 {segment.speaker_id}')}: {segment.content}"
        for segment in payload.segments
    )
    try:
        markdown = await request.app.state.summary_chain.ainvoke(
            {
                "title": payload.title,
                "purpose": payload.purpose,
                "note": payload.note,
                "transcript": transcript,
                "previous_summary": payload.previous_summary,
                "regeneration_reason": payload.regeneration_reason,
            }
        )
    except APITimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="LLM API 공급자 응답 시간이 초과되었습니다."
        ) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail="LLM API 공급자 호출에 실패했습니다.") from exc
    return Response(markdown, media_type="text/markdown")
