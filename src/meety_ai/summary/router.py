from fastapi import APIRouter, Request, Response

from meety_ai.summary.schemas import SummaryRequest

summary_router = APIRouter()


@summary_router.post("/v1/summary")
async def generate_summary(payload: SummaryRequest, request: Request) -> Response:
    speaker_names = {
        speaker.speaker_id: speaker.display_name or f"화자 {speaker.speaker_id}"
        for speaker in payload.speakers
    }
    transcript = "\n".join(
        f"{speaker_names.get(segment.speaker_id, f'화자 {segment.speaker_id}')}: {segment.content}"
        for segment in payload.segments
    )
    markdown = await request.app.state.summary_chain.ainvoke(
        {
            "title": payload.title,
            "purpose": payload.purpose,
            "note": payload.note,
            "transcript": transcript,
        }
    )
    return Response(markdown, media_type="text/markdown")
