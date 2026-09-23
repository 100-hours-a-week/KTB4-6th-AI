from langchain_core.language_models.fake_chat_models import FakeListChatModel

from meety_ai.summary.chain import create_summary_chain, summary_prompt


async def test_summary_chain_returns_markdown_for_complete_transcript():
    result_markdown = "# 회의 요약\n\n- 배포 일정을 금요일로 확정했습니다."
    model = FakeListChatModel(responses=[result_markdown])
    chain = create_summary_chain(model)
    inputs = {
        "title": "주간 회의",
        "purpose": "진행 상황 공유",
        "note": "다음 일정 확인",
        "transcript": "홍길동: 배포 일정을 금요일로 확정했습니다.",
    }

    human_prompt = summary_prompt.invoke(inputs).messages[1].content
    result = await chain.ainvoke(inputs)

    assert all(value in human_prompt for value in inputs.values())
    assert result == result_markdown
