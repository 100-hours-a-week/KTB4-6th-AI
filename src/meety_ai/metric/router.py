import asyncio
import json
import time

import structlog
from fastapi import APIRouter, HTTPException, Request
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable
from openai import APIError, APITimeoutError
from structlog.contextvars import bind_contextvars

from meety_ai.core.errors import TaskError
from meety_ai.metric.calculation import calculate_metrics
from meety_ai.metric.schemas import MetricClassification, MetricRequest, MetricResponse

logger = structlog.stdlib.get_logger(__name__)
metric_router = APIRouter()
WORK_TIMEOUT_SECONDS = 180
_ERROR_STATUS = {"timeout": 504, "provider_failed": 502}


async def run_metric(payload: MetricRequest, chain: Runnable) -> MetricResponse:
    """전사를 분류해 회의 지표를 계산한다. 실패는 TaskError로 전달한다."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(WORK_TIMEOUT_SECONDS):
            if not payload.segments:
                return calculate_metrics(payload, None)
            segments = sorted(payload.segments, key=lambda segment: segment.sequence_number)
            transcript = json.dumps(
                [
                    {"index": index, **s.model_dump(by_alias=True, exclude={"segment_id"})}
                    for index, s in enumerate(segments)
                ],
                ensure_ascii=False,
            )
            classification = await chain.ainvoke(
                {
                    "summary": payload.summary,
                    "transcript": transcript,
                    "segment_count": len(segments),
                },
                config={
                    "run_name": "meeting_metric",
                    "metadata": {
                        "meeting_id": payload.meeting_id,
                        "request_id": payload.request_id,
                    },
                },
            )
            classification = MetricClassification.model_validate(classification)
            result = calculate_metrics(payload, classification)
    except Exception as exc:
        logger.warning(
            "metric_classification_failed",
            error_type=type(exc).__name__,
            provider_status=getattr(exc, "status_code", None),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        if isinstance(exc, TimeoutError):
            raise TaskError("timeout", "지표 작업 제한 시간이 초과되었습니다.") from exc
        if isinstance(exc, APITimeoutError):
            raise TaskError("timeout", "LLM API 공급자 응답 시간이 초과되었습니다.") from exc
        if isinstance(exc, (APIError, OutputParserException, ValueError)):
            raise TaskError("provider_failed", "LLM 호출 또는 응답 검증에 실패했습니다.") from exc
        raise
    logger.info("metric_completed", duration_ms=round((time.perf_counter() - started) * 1000))
    return result


@metric_router.post("/v1/metric", response_model=MetricResponse)
async def generate_metric(payload: MetricRequest, request: Request) -> MetricResponse:
    bind_contextvars(meeting_id=payload.meeting_id)
    try:
        return await run_metric(payload, request.app.state.metric_chain)
    except TaskError as error:
        raise HTTPException(_ERROR_STATUS[error.code], detail=str(error)) from error
