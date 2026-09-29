"""연속 압축 음성을 PCM으로 변환한다."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.stdlib.get_logger(__name__)

# ffmpeg 오류 원인 확인용으로 stderr 마지막 부분만 보관한다.
MAX_STDERR_BYTES = 2048


class AudioDecodeError(Exception):
    """디코더 프로세스의 입력·출력 실패를 호출자에게 알린다."""


class AudioDecoder:
    def __init__(self, on_pcm: Callable[[bytes], Awaitable[None]]):
        self._on_pcm = on_pcm
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pcm_bytes = 0
        self._stderr_reader: asyncio.Task[None] | None = None
        self._stderr = b""

    async def _read_pcm(self) -> None:
        while chunk := await self._proc.stdout.read(65536):
            self._pcm_bytes += len(chunk)
            await self._on_pcm(chunk)

    async def _read_stderr(self) -> None:
        # 파이프가 차서 ffmpeg가 멈추지 않도록 끝까지 읽되 마지막 부분만 남긴다.
        while chunk := await self._proc.stderr.read(4096):
            self._stderr = (self._stderr + chunk)[-MAX_STDERR_BYTES:]

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
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read_pcm())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

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
        if self._proc is None:
            return
        # 강제 종료 전 종료코드로 ffmpeg 자체 실패와 정리 목적의 kill을 구분한다.
        returncode = self._proc.returncode
        if returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
        stderr_reader, self._stderr_reader = self._stderr_reader, None
        if stderr_reader is None:
            return  # 이미 정리했다.
        with contextlib.suppress(Exception):
            await stderr_reader
        if returncode not in (None, 0) or self._stderr:
            # stderr는 표준 입력 파이프 기준 메시지라 회의 내용·주소가 들어가지 않는다.
            logger.warning(
                "ffmpeg_failed",
                returncode=returncode,
                pcm_bytes=self._pcm_bytes,
                stderr=self._stderr.decode("utf-8", "replace"),
            )
