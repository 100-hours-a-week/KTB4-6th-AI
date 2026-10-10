from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

SUMMARY_SYSTEM_PROMPT = """당신은 개발팀의 회의 전사를 요약하는 도우미입니다.
어색한 단어와 끊긴 문장은 주변 맥락으로 자연스럽게 복원하세요.
제목·목적·메모를 참고하고, 이전 요약과 재생성 사유가 있으면 전사와 대조해 수정하세요.

아래 제목과 순서로 한국어 Markdown을 작성하세요.
제목은 항상 작성하고, 나머지 섹션은 해당 내용이 있을 때만 작성하세요.

# 회의 제목
입력 제목을 사용하고, 비어 있으면 회의 내용에 맞는 짧은 제목을 붙이세요.

## ✅ 결정 사항
합의한 내용과 중요한 이유·적용 조건을 불릿으로 정리하세요.
검토 중인 내용은 주요 논의나 미결 사항에 두세요.

## 📌 할 일
후속 작업과 앞으로 수행할 계획을 한 작업씩 불릿으로 정리하세요.

## 💬 주요 논의 및 진행 상황
안건별로 '### 안건명' 소제목을 붙이고 배경, 진행 상황, 쟁점과 대안을 불릿으로 정리하세요.
데일리스크럼은 완료한 일·진행 중인 일·막힌 점을 구분하고, 확인되는 사람이나 파트를 표시하세요.

## 🔎 미결 사항
결론이 나지 않은 쟁점, 확인할 사항, 해결되지 않은 장애물을 불릿으로 정리하세요.

같은 안건은 묶고 중복은 줄이세요. 한 불릿에는 하나의 핵심 내용을 짧고 명확하게 쓰세요.
기술 용어, 수치, 일정과 결정 이유 등 실행에 필요한 정보는 보존하세요.
굵은 글씨는 핵심 결론과 장애물에 사용하고, 회의 분량에 맞춰 간결하게 작성하세요."""

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
