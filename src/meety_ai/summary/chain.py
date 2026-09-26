from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

SUMMARY_SYSTEM_PROMPT = """당신은 회의 전사를 요약하는 도우미입니다.
회의 전사가 완벽하지 않다는 점을 고려하세요.
어색한 단어가 있다면 맥락에 맞는 유추를 통해 요약을 완성하세요.
이전 요약과 재생성 사유가 주어지면 전사와 대조해 수정하세요.
전사에 없는 사실을 이전 요약이나 재생성 사유만을 근거로 추가하지 마세요.
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


def build_summary_prompt(inputs: dict[str, str | None]):
    prompt = summary_prompt.invoke(inputs)
    feedback = []
    if previous_summary := inputs.get("previous_summary"):
        feedback.append(f"이전 요약:\n{previous_summary}")
    if regeneration_reason := inputs.get("regeneration_reason"):
        feedback.append(f"재생성 요청 사유:\n{regeneration_reason}")
    if feedback:
        prompt.messages.append(HumanMessage(content="\n\n".join(feedback)))
    return prompt


def create_summary_chain(model: BaseChatModel) -> Runnable:
    return RunnableLambda(build_summary_prompt) | model | StrOutputParser()
