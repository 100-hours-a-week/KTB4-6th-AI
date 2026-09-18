# #12: 실제 FFmpeg 디코딩 검증

# 테스트 이름: test_streaming_pcm_and_final_flush
# 목적: 두 입력 형식의 연속 변환·잔여 출력·자원 정리를 확인한다.
# 준비: WebM+Opus와 MP4+AAC의 여러 청크, 동일 전체 입력을 변환한 기준 PCM.
# 준비: 기준 PCM 규격은 16kHz·mono·s16le이며 입력 자료의 출처를 기록한다.
# 실행: 형식을 parametrize → start → 순서대로 feed → 종료 전 PCM 수신 → finish → close.
# 기대 결과: finish 전에 PCM이 수신되고 최종 PCM을 합치면 기준 결과와 일치한다.
# 기대 결과: close 후 해당 FFmpeg 프로세스와 출력 읽기 작업이 종료된다.
# 실패 조건: 종료 전 출력 없음, 최종 PCM 누락·중복·변형, 종료 후 자원 잔류.
# 주의: 입력 청크 하나와 출력 PCM 블록 하나가 대응한다고 가정하지 않는다.
# 주의: asyncio.timeout으로 대기를 제한하고 실패해도 finally에서 close한다.
# 핵심 API: AudioDecoder, asyncio.Event, asyncio.timeout, pytest.mark.parametrize.
