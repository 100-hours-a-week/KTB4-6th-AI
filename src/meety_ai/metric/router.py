import json
import time

import structlog
from fastapi import APIRouter, HTTPException, Request
from langchain_core.exceptions import OutputParserException
from openai import APIError, APITimeoutError
from structlog.contextvars import bind_contextvars

from meety_ai.metric.calculation import calculate_metrics
from meety_ai.metric.schemas import MetricClassification, MetricRequest, MetricResponse

logger = structlog.stdlib.get_logger(__name__)
metric_router = APIRouter()


@metric_router.post("/v1/metric", response_model=MetricResponse)
async def generate_metric(payload: MetricRequest, request: Request) -> MetricResponse:
    bind_contextvars(meeting_id=payload.meeting_id)
    if not payload.segments:
        return calculate_metrics(payload, None)
    transcript = json.dumps(
        [
            s.model_dump(by_alias=True)
            for s in sorted(payload.segments, key=lambda segment: segment.sequence_number)
        ],
        ensure_ascii=False,
    )
    started = time.perf_counter()
    try:
        classification = await request.app.state.metric_chain.ainvoke(
            {"summary": payload.summary, "transcript": transcript},
            config={
                "run_name": "meeting_metric",
                "metadata": {"meeting_id": payload.meeting_id, "request_id": payload.request_id},
            },
        )
        classification = MetricClassification.model_validate(classification)
        result = calculate_metrics(payload, classification)
    except Exception as exc:
        logger.warning(
            "metric_classification_failed",
            error_type=type(exc).__name__,
            provider_status=getattr(exc, "status_code", None),
        )
        if isinstance(exc, (APITimeoutError, TimeoutError)):
            raise HTTPException(
                status_code=504, detail="LLM API 공급자 응답 시간이 초과되었습니다."
            ) from exc
        if isinstance(exc, (APIError, OutputParserException, ValueError)):
            raise HTTPException(
                status_code=502, detail="LLM 호출 또는 응답 검증에 실패했습니다."
            ) from exc
        raise
    logger.info("metric_completed", duration_ms=round((time.perf_counter() - started) * 1000))
    return result
