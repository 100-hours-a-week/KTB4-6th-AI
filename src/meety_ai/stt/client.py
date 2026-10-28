"""STT 공급자가 구현할 공통 수명주기와 오류를 정의한다."""

from typing import Protocol


class STTProviderError(Exception):
    """STT 공급자 오류를 원본 응답 노출 없이 호출자에게 알린다."""


class STTClient(Protocol):
    """구현 클래스는 연결 설정과 STTTranscript를 전달할 콜백을 받는다."""

    async def start(
        self, *, meeting_id: int = 0, recording_session_id: int = 0, context: str = ""
    ) -> None:
        """오디오를 전송할 수 있도록 전사 세션을 시작한다."""
        ...

    async def send_audio(self, chunk: bytes) -> None:
        """16 kHz mono pcm_s16le 청크를 전송한다."""
        ...

    async def update_context(self, context: str) -> None:
        """이후 전사에 사용할 맥락을 교체한다. 이미 확정된 전사는 바꾸지 않는다."""
        ...

    async def finish(self) -> None:
        """입력 종료를 알리고 마지막 확정 전사 콜백까지 기다린다."""
        ...

    async def close(self) -> None:
        """정상 종료 여부와 관계없이 연결과 백그라운드 작업을 정리한다."""
        ...
