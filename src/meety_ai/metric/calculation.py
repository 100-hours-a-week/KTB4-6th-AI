from meety_ai.metric.schemas import (
    AgendaMetric,
    DurationMetric,
    MetricClassification,
    MetricRequest,
    MetricResponse,
    SegmentRange,
)
from meety_ai.summary.schemas import TranscriptionSegment


def union_duration(segments: list[TranscriptionSegment]) -> int:
    total = 0
    end = 0
    for segment in sorted(segments, key=lambda segment: segment.started_at_ms):
        total += max(segment.ended_at_ms - max(segment.started_at_ms, end), 0)
        end = max(end, segment.ended_at_ms)
    return total


def select_ranges(
    segments: list[TranscriptionSegment], ranges: list[SegmentRange]
) -> list[TranscriptionSegment]:
    positions = {segment.segment_id: index for index, segment in enumerate(segments)}
    selected = set()
    for interval in ranges:
        start = positions.get(interval.start_segment_id)
        end = positions.get(interval.end_segment_id)
        if start is None or end is None or end < start:
            raise ValueError("모델이 존재하지 않거나 역순인 전사 범위를 반환했습니다.")
        selected.update(range(start, end + 1))
    return [segment for index, segment in enumerate(segments) if index in selected]


def calculate_metrics(
    payload: MetricRequest,
    classification: MetricClassification | None,
) -> MetricResponse:
    segments = sorted(payload.segments, key=lambda segment: segment.sequence_number)
    agendas = None
    off_topic_duration = None
    if classification is not None:
        agendas = [
            AgendaMetric(
                title=agenda.title,
                duration_ms=union_duration(select_ranges(segments, agenda.ranges)),
            )
            for agenda in classification.agendas
        ]
        off_topic_segments = select_ranges(
            segments, [interval for item in classification.off_topic for interval in item.ranges]
        )
        off_topic_duration = union_duration(off_topic_segments)
    return MetricResponse(
        request_id=payload.request_id,
        meeting_id=payload.meeting_id,
        off_topic=DurationMetric(duration_ms=off_topic_duration),
        agendas=agendas,
    )
