import pytest
from langchain_core.messages import HumanMessage

from meety_ai.chat.agent import ChatGenerationError, _create_middleware, validate_citations
from meety_ai.chat.schemas import ChatAnswer


async def test_model_input_size_check_accepts_valid_messages():
    # 입력 크기 검사 오타로 모든 정상 요청이 생성 전에 실패했던 문제를 보호한다.
    middleware = _create_middleware({42}, 8, 8, 256 * 1024)[0]

    await middleware.abefore_model({"messages": [HumanMessage(content="배포 일정은?")]}, None)


@pytest.mark.parametrize(
    "citation",
    [
        {"sourceType": "summary", "meetingId": 7, "summaryId": 1},
        {"sourceType": "transcript", "meetingId": 42, "segmentId": 99},
    ],
)
def test_answer_rejects_citation_to_unprovided_source(citation):
    answer = ChatAnswer(answer="금요일입니다.", citations=[citation])

    with pytest.raises(ChatGenerationError) as caught:
        validate_citations(answer, {("transcript", 42, 0)})

    assert caught.value.code == "generation_failed"


@pytest.mark.parametrize(
    "citations",
    [
        [],
        [{"sourceType": "transcript", "meetingId": 42, "segmentId": 0}],
        [{"sourceType": "summary", "meetingId": 7, "summaryId": 1}],
    ],
)
def test_answer_accepts_only_provided_sources_or_no_citations(citations):
    answer = ChatAnswer(answer="확인할 자료가 부족합니다.", citations=citations)

    validate_citations(answer, {("transcript", 42, 0), ("summary", 7, 1)})
