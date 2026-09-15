"""연속 압축 음성을 PCM으로 변환한다."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class AudioDecodeError(Exception):
    """디코더 프로세스의 입력·출력 실패를 호출자에게 알린다."""


class AudioDecoder:
    def __init__(self, on_pcm: Callable[[bytes], Awaitable[None]]):
        self._on_pcm = on_pcm
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pcm_bytes = 0

    async def _read_pcm(self) -> None:
        while chunk := await self._proc.stdout.read(65536):
            self._pcm_bytes += len(chunk)
            await self._on_pcm(chunk)

    async def start(self) -> None:
        """ffmpeg 프로세스를 생성해 입출력을 연결한다."""
        self._proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",  # stdin으로 압축 음성을 받는다.
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",  # stdout으로 16kHz mono s16le PCM을 내보낸다.
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read_pcm())

    async def feed(self, chunk: bytes) -> None:
        """압축 음성 청크를 디코더 입력에 순서대로 전달한다."""
        if self._proc is None or self._reader is None:
            raise RuntimeError("start()를 먼저 호출해야 합니다.")
        if self._reader.done():
            raise AudioDecodeError("디코더 출력이 중단되었습니다.")
        try:
            self._proc.stdin.write(chunk)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise AudioDecodeError("디코더 입력이 닫혔습니다.") from e

    async def finish(self) -> None:
        """입력 종료를 알리고 잔여 PCM 전달과 프로세스 종료를 확인한다."""
        if self._proc is None or self._reader is None:
            raise RuntimeError("start()를 먼저 호출해야 합니다.")
        self._proc.stdin.close()
        try:
            await self._reader
        except Exception as e:
            raise AudioDecodeError("PCM 출력 처리에 실패했습니다.") from e
        returncode = await self._proc.wait()
        if returncode != 0 or self._pcm_bytes == 0:
            raise AudioDecodeError(f"디코딩에 실패했습니다. (종료코드 {returncode})")

    async def close(self) -> None:
        """오류·취소·연결 해제 시 reader와 ffmpeg 프로세스를 정리한다."""
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader  # 취소 완료 대기 + 남은 예외 회수
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
