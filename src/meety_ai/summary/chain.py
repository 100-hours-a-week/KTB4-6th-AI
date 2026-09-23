from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

SUMMARY_SYSTEM_PROMPT = """당신은 회의 전사를 요약하는 도우미입니다.
회의 전사가 완벽하지 않다는 점을 고려하세요. 어색한 단어가 있다면 맥락에 맞는 유추를 통해 요약을 완성하세요.
결과는 Markdown으로 작성하세요."""

SUMMARY_HUMAN_PROMPT = """제목: {title}
목적: {purpose}
메모: {note}

전사:
{transcript}"""

summary_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", SUMMARY_SYSTEM_PROMPT),
        ("human", SUMMARY_HUMAN_PROMPT),
    ]
)


def create_summary_chain(model: BaseChatModel) -> Runnable:
    return summary_prompt | model | StrOutputParser()
