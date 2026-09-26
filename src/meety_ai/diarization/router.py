import asyncio
from typing import TypedDict

import modal
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import TypeAdapter, ValidationError, with_config

from meety_ai.diarization.schemas import (
    AttributedSegment,
    DiarizationRequest,
    DiarizationResponse,
    DiarizationSegment,
)

logger = structlog.stdlib.get_logger(__name__)

diarization_router = APIRouter()

# Modal 함수가 반환한 처리 실패 코드별 응답 상태.
_MODAL_ERROR_STATUS = {"download_failed": 502, "decode_failed": 422, "inference_failed": 502}


@with_config(strict=True)
class SpeakerInterval(TypedDict):
    """Modal이 반환한 화자 구간. 참가자와 연결되지 않은 회의 내 화자 ID다."""

    speaker_id: str
    start_ms: int
    end_ms: int


_INTERVALS = TypeAdapter(list[SpeakerInterval])


async def diarize(audio_url: str, timeout_seconds: float) -> list[SpeakerInterval]:
    """배포된 Modal 화자 분리 함수를 호출해 원본 화자 구간을 반환한다.

    예외 메시지에 URL이 섞일 수 있어 로그에는 예외 타입만 남긴다.
    """
    try:
        # 함수 조회도 SDK 호출이므로 인증·미배포 오류를 같은 경계에서 처리한다.
        function = modal.Function.from_name("meety-diarization", "diarize")
        async with asyncio.timeout(timeout_seconds):
            result = await function.remote.aio(audio_url)
    except (TimeoutError, modal.exception.TimeoutError) as exc:
        logger.warning("diarization_timeout", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=504, detail="화자 분리 응답 시간이 초과되었습니다."
        ) from None
    except modal.exception.Error as exc:
        logger.warning("diarization_call_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="화자 분리 호출에 실패했습니다.") from None

    if isinstance(result, dict) and result.get("status") == "error":
        error_code = result.get("error_code")
        if error_code not in _MODAL_ERROR_STATUS:
            error_code = "unknown"
        logger.warning("diarization_failed", error_code=error_code)
        raise HTTPException(
            status_code=_MODAL_ERROR_STATUS.get(error_code, 502),
            detail={"errorCode": error_code},
        )
    try:
        if result["status"] != "ok":
            raise ValueError("알 수 없는 status")
        # ValidationError는 ValueError의 하위 타입이다.
        intervals = _INTERVALS.validate_python(result["intervals"])
        if any(item["start_ms"] >= item["end_ms"] for item in intervals):
            raise ValueError("화자 구간의 시작 시각은 종료 시각보다 빨라야 합니다.")
        return intervals
    except (TypeError, KeyError, ValueError) as exc:
        logger.warning("diarization_invalid_response", error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail={"errorCode": "invalid_response"}) from None


def _merge_by_speaker(intervals: list[SpeakerInterval]) -> dict[str, list[tuple[int, int]]]:
    """같은 화자의 겹치는 구간을 합쳐 겹침 시간이 두 번 더해지지 않게 한다."""
    merged: dict[str, list[tuple[int, int]]] = {}
    for item in sorted(intervals, key=lambda i: i["start_ms"]):
        spans = merged.setdefault(item["speaker_id"], [])
        if spans and item["start_ms"] <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], item["end_ms"]))
        else:
            spans.append((item["start_ms"], item["end_ms"]))
    return merged


def attribute_speakers(
    segments: list[DiarizationSegment], intervals: list[SpeakerInterval]
) -> list[AttributedSegment]:
    """전사 구간마다 겹친 시간이 가장 긴 화자 한 명을 대표 화자로 연결한다.

    겹침이 없거나 최장 화자가 동률이면 판단 불가로 보고 null을 둔다.
    전사와 오디오의 시간축은 같다고 가정한다.
    """
    # ponytail: 전사 구간마다 전체 화자 구간을 훑는 O(전사×구간)이다.
    # 긴 회의에서 느려지면 정렬된 구간을 이동 창으로 훑도록 바꾼다.
    merged = _merge_by_speaker(intervals)
    result = []
    for segment in segments:
        totals = {
            speaker_id: sum(
                max(0, min(end, segment.ended_at_ms) - max(start, segment.started_at_ms))
                for start, end in spans
            )
            for speaker_id, spans in merged.items()
        }
        ranked = sorted(totals.values(), reverse=True)
        speaker_id = None
        if ranked and ranked[0] > 0 and (len(ranked) == 1 or ranked[0] > ranked[1]):
            speaker_id = max(totals, key=totals.__getitem__)
        result.append(AttributedSegment(**segment.model_dump(), speaker_id=speaker_id))
    return result


@diarization_router.post("/v1/diarization")
async def generate_diarization(request: Request) -> DiarizationResponse:
    # 기본 422 응답은 입력 원문을 포함하므로 presigned URL이 노출되지 않도록 직접 검증한다.
    try:
        payload = DiarizationRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(status_code=422, detail=detail) from None
    intervals = await diarize(
        payload.audio_url, request.app.state.settings.diarization_timeout_seconds
    )
    return DiarizationResponse(
        meeting_id=payload.meeting_id,
        segments=attribute_speakers(payload.segments, intervals),
    )
