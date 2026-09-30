# ADR-0008: 실시간 AI 세션을 녹음 전체 동안 유지하고 녹음 스트림마다 디코더를 교체한다

## Status

Accepted

---

## Context

녹음 담당자의 브라우저는 MediaRecorder로 녹음한 압축 음성 청크를 FE–BE WebSocket으로 보내고, BE는 이를 BE–AI WebSocket으로 실시간 AI에 전달한다. 실시간 AI는 [ADR-0007](0007-live-audio-decoder-ffmpeg-subprocess.md)에 따라 ffmpeg 서브프로세스로 PCM을 만들어 STT에 넣는다. 현재는 FE 연결, AI 연결, AI 세션, ffmpeg 프로세스, STT 세션이 모두 한 번의 녹음 연결과 수명을 같이한다.

```text
Browser ─(WS)─▶ BE ─(WS)─▶ AI 세션 ─▶ ffmpeg ─PCM─▶ STT(+화자 분리)
MediaRecorder                         └ 현재는 모두 연결 하나와 수명이 같다
```

모바일에서 화면이 꺼지거나 페이지가 새로고침되면 FE–BE 연결이 끊기고, 이때 MediaRecorder도 함께 멈춘다. 다시 녹음하려면 새 MediaRecorder를 만들어야 하고, 새 MediaRecorder는 **헤더부터 다시 시작하는 새 스트림**을 만든다. 이 ADR은 녹음 도중 새 스트림이 생길 때 AI 세션과 디코더를 어떤 단위로 유지하고 교체할지 결정한다.

현재 코드에서 확인한 제약은 다음과 같다.

- BE는 FE 재연결 핸드셰이크에서 항상 새 AI 연결을 시작한다. 기존 AI 연결이 남아 있으면 `registry.reserve`가 실패해 503으로 거절하고, 옛 FE 세션이 남아 있으면 409로 거절한다. ([AudioWebSocketHandshakeInterceptor](https://github.com/100-hours-a-week/KTB4-6th-BE/blob/dev/src/main/java/com/backend/meety/domain/recording/realtime/AudioWebSocketHandshakeInterceptor.java))
- BE는 AI의 `session.ended`를 받으면 전사 확정 이벤트를 발행하고, 이 이벤트가 요약을 시작한다. ([AiLiveMeetingInboundHandler](https://github.com/100-hours-a-week/KTB4-6th-BE/blob/dev/src/main/java/com/backend/meety/domain/ai/realtime/AiLiveMeetingInboundHandler.java))
- BE는 전사 중복을 `recordingSessionId:sequenceNumber`로 판정한다. 새 AI 세션이 전사 번호를 0부터 다시 세면 이후 전사가 중복으로 버려진다. ([AiTranscriptSegmentMessage](https://github.com/100-hours-a-week/KTB4-6th-BE/blob/dev/src/main/java/com/backend/meety/domain/ai/realtime/AiTranscriptSegmentMessage.java))
- `audio.meta`의 sequence는 BE가 AI 연결 단위로 증가시키는 값이다. FE 재연결과는 무관하다.
- 사용자 일시정지는 현재 같은 MediaRecorder를 `pause()`/`resume()`하고, FE–BE WebSocket은 유지한다. 일시정지 중에는 WebSocket에 트래픽이 없다.

---

## Decision Drivers

- **FE 연결이 끊겼다가 복구돼도 같은 녹음에 전사를 이어 붙일 수 있는가?**  연결 끊김, 새로고침에도 계속 전사가 이어질 수 있어야 한다.
- **실시간 화자 분리의 일관성을 유지할 수 있는가?** 현재는 화자 분리가 회의 종료 후 한번에 진행되지만, 향후 실시간 화자분리 기능으로 변경될 수 있다. 화자 분리의 화자 id는 세션 안에서만 유지되므로, 재연결이나 일시정지를 할 떄마다 세션이 초기화되면 일관성이 깨진다. 화자분리 방식 변경에 확장성 있는 구조가 필요하다.
- **일시정지와 연결 끊김을 하나의 경로로 처리할 수 있는가?** 처리 규칙의 수를 줄이고, 자주 발생하지 않는 연결 끊김 복구 경로를 자주 발생하는 일시정지 처리 경로와 통일함으로써 복구 경로의 버그 확인을 쉽게 한다.
- **전사 시간과 녹음 파일의 시간축이 일치하는가?** 사후 화자 분리는 녹음 파일과 전사 구간의 시작·종료 시각을 맞춰 동작하기 때문에 전사 시간과 녹음 파일의 시간축이 일치되어야 한다.
- **현재 구조(FE–BE–AI 중계, MediaRecorder 압축 전송)를 유지할 수 있는가?** 연결 구조와 전송 형식을 크게 바꾸지 않는다.

---

## Considered Options

### A. 새 스트림마다 새 AI 세션을 연다

FE가 다시 연결되면 BE가 기존 AI 연결을 닫고 새 AI 연결에서 `session.start`를 보낸다. 새 세션은 새 ffmpeg와 새 STT 세션을 가진다. 전사가 이어지도록 BE가 `sequence_start`(저장된 최대 transcription id + 1)와 `time_offset_ms`(앞선 오디오 길이)를 넘긴다.

재연결에만 적용할지, 일시정지에도 적용할지 두가지 세부 방안으로 나뉠 수 있다.

- 재연결에만 적용하면 사용자 일시정지는 지금처럼 `session.pause`/`session.resume`으로 세션을 유지한다.
- 일시정지에도 적용하면 일시정지할 때 `session.stop`으로 세션을 끝내고 재개할 때 새 `session.start`를 보낸다. 이 경우 AI 프로토콜에서 일시정지 상태를 없앨 수 있다.

**장점**

- AI는 "연결 하나 = 세션 하나"라는 지금 구조를 그대로 유지한다.
- 재연결 때 BE가 기존 AI 연결 상태를 따질 필요가 없다.

**단점**

- **새 세션을 열 때마다 STT·화자 분리 세션이 초기화된다.** 화자 번호의 일관성이 재연결 단위로 깨지고, 일시정지에도 적용하면 일시정지마다 깨진다.
- 새 세션마다 STT 공급자 연결을 새로 맺어야 해서 재개가 늦어진다.
- 옛 세션을 `session.stop`으로 끝내면 `session.ended`가 요약을 시작하므로, BE에 "중간 종료" 분기가 필요하다.
- `time_offset_ms`를 BE가 정확히 계산해야 한다. 실제로 AI에 도착한 오디오 길이를 BE가 알 수 없어 오차가 생긴다.

### B. AI 세션을 유지하고 녹음 스트림마다 디코더를 교체한다

BE–AI 연결과 AI 세션(STT·화자 분리 세션 포함)은 녹음 시작부터 녹음 종료까지 하나로 유지한다. FE가 다시 연결되면 BE는 기존 AI 연결에 오디오 청크를 다시 전송한다. **BE는 새 스트림의 오디오를 보내기 전에** `decoder.reset`**을 보내고, AI는 기존 ffmpeg를** `finish()`**한 뒤 새 ffmpeg 프로세스를 띄워** `decoder.ready`**로 응답한다.**

```text
[AI 세션 하나: STT·화자 분리 세션 유지]
 스트림1 → ffmpeg #1 ─┐
 스트림2 → ffmpeg #2 ─┼→ PCM → STT(+화자 분리)
 스트림3 → ffmpeg #3 ─┘   (decoder.reset마다 교체)
```

**장점**

- **STT·화자 분리 세션이 녹음 전체 동안 이어진다.** 재연결해도 실시간 AI서버의 녹음 세션은 초기화되지 않기 때문에 화자 번호, 전사 번호, 시간축이 모두 이어져 일관성을 쉽게 유지할 수 있다.
- FE 녹음 스트림마다 PCM Decoder와 ffmpeg 프로세스가 새로 시작하므로 하나의 ffmpeg 프로세스에 오디오 헤더가 두번 들어가는 문제가 발생하지 않는다.

**단점**

- BE가 FE 재연결 때 기존 AI 연결을 재사용하도록 바뀌어야 한다. 핸드셰이크 처리, 기존 FE 세션 대체가 필요하다.
- FE가 끊긴 동안 STT 세션이 오디오 없이 열려 있다. 공급자가 오디오 없는 세션을 얼마나 유지하는지 확인 후 처리가 필요하다.
- AI 세션 자체가 죽는 경우(AI 장애, STT 오류, 장시간 끊김 timeout)에는 새 세션이 필요하다. 이 경우 별도의 대응 방안이 필요하다.

### C. AI 세션과 디코더를 모두 유지하고 새 스트림을 그대로 이어 넣는다

새 스트림의 청크를 기존 ffmpeg에 그대로 이어 넣는다. 변형으로, 첫 헤더를 저장해 두었다가 이후 스트림의 헤더를 대신하는 방법도 있다.

**장점**

- AI와 BE 모두 스트림 경계를 알 필요가 없다.

**단점**

- **Fragmented MP4에서는 스트림 경계마다 오디오 일부가 오류 없이 사라진다.** 실험 결과 브라우저 실시간 출력에선 0.04\~0.12초의 음성이 유실되었고, 합성 음원에서는 뒤에 이어진 파일 전체가 유실되었다. 손실 크기가 입력 구조에 따라 달라 예측할 수 없다.
- **녹음 형식이 바뀌면 이후 오디오가 오류 없이 모두 사라진다.** Chrome webm 뒤에 Safari mp4를 이어 넣거나 그 반대로 넣으면, ffmpeg가 두 번째 스트림을 통째로 버리면서도 입력은 계속 받는다. AI 세션은 정상으로 보이지만 회의가 끝날 때까지 전사가 나오지 않는다. 녹음 담당자가 PC에서 폰으로 바꿔 재연결하면 발생할 수 있다.
- **문제가 생겨도 감지하기 어렵다.** 오디오가 유실된 상황에서요 ffmpeg 종료코드는 0으로 정상 종료되었고, Safari mp4 → Chrome webm 경우는 경고도 발생하지 않았다.
- **재연결할수록 전사 시간과 녹음 파일의 시간이 어긋난다.** 같은 형식 스트림 5개(재연결 4번)를 이어 넣었을 때 Chrome mp4는 0.51초, Safari mp4는 0.17초가 누적으로 사라졌다. 사라진 만큼 전사 시간이 녹음 파일보다 앞당겨져, 사후 화자 분리와 타임스탬프가 맞지 않는다.
- 헤더를 재사용하려면 MediaRecorder 청크 경계가 Cluster/fragment 경계와 맞지 않는 문제를 풀어야 한다. webm과 MP4 각각의 파서가 필요하다.

---

## Decision

**B안을 선택해 AI 세션을 녹음 전체 동안 유지하고, 녹음 스트림마다 ffmpeg 디코더만 교체한다.**

- 화자 분리 일관성이 결정적인 기준이다. A안은 재연결이나 일시정지마다 STT·화자 분리 세션을 초기화하므로, 향후 도입 가능성이 있는 실시간 화자 분리에 적합하지 않다.
- C안은 fractured MP4에서 스트림 경계마다 오디오가 오류 없이 유실되는 것을 브라우저 출력으로 확인해 제외한다.
- 가장 유력한 대안인 A안은 AI 변경이 가장 작지만, 세션 초기화 문제에 더해 BE가 정확히 알 수 없는 시간 오프셋에 의존한다. B안은 시간축이 STT에 들어간 오디오 기준으로 자동으로 맞는다.

세부 규칙은 다음과 같다.

- **스트림 단위**: FE–BE WebSocket 연결 하나에 MediaRecorder 하나, AI 디코더 하나가 대응한다.
- **일시정지**: FE는 PATCH PAUSED 후 MediaRecorder를 `stop()`하고, 마지막 청크를 보낸 뒤 FE–BE WebSocket을 닫는다. 재개하면 새 MediaRecorder와 새 WebSocket을 연다. 디코더 교체는 일시정지와 재연결이 같은 경로가 된다.
- **일시정지 신호 유지**: BE–AI의 `session.pause`/`session.resume`은 유지한다. 사용자의 명시적인 정지·재개와 의도하지 않은 끊김·재연결을 AI가 구분할 수 있어야, 이후 정지 중 외부 API 연결 유지 등의 정책을 따로 정할 수 있다.
- **스트림 경계 신호**: BE는 FE–BE WebSocket 핸드셰이크마다(첫 연결 포함) 기존 AI 연결에 `decoder.reset`을 보내고 `decoder.ready`를 받은 뒤 오디오를 전달한다. 재개할 때는 `session.resume` → `session.resumed` → `decoder.reset` → `decoder.ready` 순서다. FE→BE 프로토콜은 바꾸지 않는다.
- **녹음 형식**: 스트림마다 형식이 바뀔 수 있으므로 `audioFormat`은 `session.start`가 아니라 `decoder.reset`의 payload로 전달한다.
- **순서 보장**: BE는 이전 FE 세션의 청크를 모두 AI에 전달한 뒤 `decoder.reset`을 보낸다.
- **이전 디코더 종료 실패**: `finish()`가 실패해도 STT 세션은 유지되므로 로그만 남기고 새 디코더로 교체한다.
- **검증**: AI는 `decoder.reset` 뒤 스트림 앞 8바이트를 모은 뒤 `audioFormat`의 헤더와 일치하는지 판별하고, 일치하지 않으면 프로토콜 오류로 처리한다. Chrome webm은 첫 청크가 1바이트뿐이라 첫 청크 하나로 판별하면 안 된다.

- **`audio.meta.sequence`**: AI 세션 안에서 계속 증가하며 스트림이 바뀌어도 초기화하지 않는다.

---

## Consequences

### Positive

- 재연결과 일시정지를 거쳐도 STT·화자 분리 세션, 전사 번호, 시간축이 이어진다.
- 전사 시간과 스트림들을 이어 붙인 녹음 파일의 시간이 일치한다.
- 재개할 때마다 디코더 교체 경로가 실행되므로, 드문 재연결 경로의 버그가 평소 사용에서 드러난다.

### Negative

- FE-BE 핸드셰이크에서 기존 AI 연결 재사용(없거나 닫혔을 때만 새로 시작), 같은 녹음의 새 FE 연결로 기존 연결 대체, `decoder.reset` 전송 등의 BE 추가 작업이 필요하다.
- 재개할 때 WebSocket 핸드셰이크, `decoder.reset` 왕복, 이전 ffmpeg 종료와 새 ffmpeg 시작 latency가 추가된다.
- 스트림 경계마다 코덱 패딩(AAC priming, opus pre-skip)이 몇 ms씩 들어간다. 녹음 파일과 STT가 같은 바이트를 디코딩하므로 둘 사이의 시간은 어긋나지 않는다.
- FE가 끊긴 동안 STT 세션이 오디오 없이 열려 있다. 공급자의 무음 세션 유지 한도와 과금에 대한 확인과 처리가 필요하다.
- AI 세션이 새로 열리는 경로(AI 장애, STT 오류, 장시간 끊김 timeout)에는 별도 처리가 필요하다.

---

## Reconsideration Conditions

- STT 공급자나 자체 모델이 오디오 없는 세션을 충분히 유지하지 못해 AI 세션 재시작이 잦아지면, A안의 이어받기를 기본 경로로 재검토한다.
- 끊긴 구간의 유실이 문제가 되거나 전송 형식을 바꿀 수 있게 되면, AudioWorklet PCM 전송과 seq·ACK 방식을 재검토한다.
- 재개할 때의 핸드셰이크 지연이 사용성 문제로 관측되면, 일시정지 중 WebSocket 유지 방식을 재검토한다.
