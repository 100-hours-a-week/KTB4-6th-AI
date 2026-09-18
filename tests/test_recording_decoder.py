import asyncio

import pytest

from meety_ai.recording.decoder import AudioDecodeError, AudioDecoder


# 테스트 이름: test_streaming_pcm_and_final_flush
# 목적: 두 입력 형식의 연속 변환과 finish 시 잔여 PCM 출력을 확인한다.
# 준비: 입력 fixture(WebM+Opus, 조각 MP4+AAC)와 형식별 기준 PCM, 수신 목록, asyncio.Event.
# 실행: 형식을 parametrize → start → 입력을 4096 bytes씩 잘라 순서대로 feed
#       → Event로 첫 PCM 수신 대기(기한 있음) → finish.
# 기대 결과: finish 전에 PCM이 한 번 이상 수신된다.
# 기대 결과: 수신한 PCM을 모두 이으면 기준 PCM과 bytes 단위로 같다.
# 실패 조건: 종료 전 출력이 없거나, 최종 PCM이 누락·중복·변형된다.
# 주의: 입력 청크 하나와 출력 PCM 블록 하나가 대응한다고 가정하지 않는다.
# 주의: 대기는 asyncio.timeout으로 제한하고, 실패해도 finally에서 close한다.
# 핵심 API: AudioDecoder, asyncio.Event, asyncio.timeout, pytest.mark.parametrize.
@pytest.mark.parametrize("audio_format", ["webm_opus", "mp4_aac"])
async def test_streaming_pcm_and_final_flush(audio_samples, audio_format):
    encoded, reference_pcm = audio_samples[audio_format]
    received = []
    first_pcm = asyncio.Event()

    async def on_pcm(chunk):
        received.append(chunk)
        first_pcm.set()

    decoder = AudioDecoder(on_pcm)
    try:
        async with asyncio.timeout(10):
            await decoder.start()
            for start in range(0, len(encoded), 4096):
                await decoder.feed(encoded[start : start + 4096])

            # 입력을 닫기 전에도 PCM이 출력되어야 함
            await first_pcm.wait()

            await decoder.finish()

        assert b"".join(received) == reference_pcm
    finally:
        await decoder.close()


# 테스트 이름: test_invalid_audio_raises_decode_error
# 목적: 디코딩할 수 없는 입력이 PCM 없이 AudioDecodeError로 끝나는지 확인한다.
# 준비: 음성이 아닌 bytes(예: b"not audio" * 100)와 수신 목록.
# 실행: start → feed → pytest.raises(AudioDecodeError) 안에서 finish.
# 기대 결과: AudioDecodeError가 발생하고 수신 PCM이 없다.
# 실패 조건: 오류 없이 끝나거나, 다른 예외가 나거나, 깨진 입력에서 PCM이 전달된다.
# 주의: pytest.raises 안에는 finish만 둔다. start·feed가 실패하면 원인이 달라진다.
# 주의: 실패해도 finally에서 close한다.
async def test_invalid_audio_raises_decode_error():
    received = []

    async def on_pcm(chunk):
        received.append(chunk)

    decoder = AudioDecoder(on_pcm)
    try:
        async with asyncio.timeout(10):
            await decoder.start()
            await decoder.feed(b"not audio" * 100)
            with pytest.raises(AudioDecodeError):
                await decoder.finish()
            assert received == []
    finally:
        await decoder.close()


# 테스트 이름: test_close_without_finish_stops_process
# 목적: stop 없이 연결이 끊긴 경우 close()만으로 ffmpeg와 reader가 정리되는지 확인한다.
# 준비: WebM 입력의 앞부분 일부.
# 실행: start → 일부 feed → finish 없이 close (asyncio.timeout으로 기한 설정).
# 기대 결과: close가 기한 안에 끝나고 프로세스 종료코드가 설정되며 reader 작업이 끝나 있다.
# 실패 조건: close가 멈추거나, 프로세스·reader가 남는다.
# 주의: 공개 API로 관찰할 수 없어 decoder._proc.returncode, decoder._reader.done()을 확인한다.
# 주의: finish 뒤 close는 finish가 이미 프로세스 종료를 기다리므로 따로 검증하지 않는다.
async def test_close_without_finish_stops_process(audio_samples):
    encoded = audio_samples["webm_opus"][0]

    async def on_pcm(chunk):
        pass

    decoder = AudioDecoder(on_pcm)
    try:
        async with asyncio.timeout(10):
            await decoder.start()
            await decoder.feed(encoded[:4096])
            await decoder.close()
        assert decoder._proc.returncode is not None
        assert decoder._reader.done()

    finally:
        await decoder.close()
