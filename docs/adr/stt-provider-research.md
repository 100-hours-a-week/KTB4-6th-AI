# STT 공급자 조사

## 결론

**아직 STT 공급자 ADR 결정은 없다. 이 문서는 Proposed 논의 단계의 연구 노트이며, 어떤 공급자도 Accepted로 기록하지 않는다.** 우선순위는 **realtime gold 정확도 → 비용 → rate/concurrency limit**이며, 화자분리(speaker diarization)는 별도의 중요한 요구사항이다. timestamp는 word-level이 아니어도 **utterance-level이면 허용**한다.

같은 first-600초, 16 kHz mono `pcm_s16le`, 1x pacing의 최신 로컬 run에서 Speechmatics Enhanced는 15.4791%, ElevenLabs realtime은 15.7599%, Soniox clean rerun은 16.0056% CER이었다. Soniox는 정확히 600초를 송신했으나 `request_timeout`으로 `finished` 응답을 받지 못했고 transcript의 max end가 596.28초였다. 따라서 Soniox의 16.0056%는 **provisional 정확도**이고, 세션 완료 실패는 **정확도와 분리한 안정성 실패**로 기록한다.

**Speechmatics Enhanced realtime**, **ElevenLabs `scribe_v2_realtime`**, **Soniox `stt-rt-v5`**를 최종 3개 provisional 후보로 둔다. Speechmatics와 ElevenLabs는 word-level timestamp, Soniox는 word 또는 sub-word token timestamp를 제공하므로 세 후보 모두 utterance-level 요구는 충족한다. Deepgram realtime(23.6925%)과 Gemini Live(first-600초 26.7111%)는 이번 최종 3후보에서 **정확도 사유로 제외**하되, 기존 screened evidence와 측정값은 삭제하지 않고 보존한다.

## Decision Drivers

1. 같은 realtime 입력과 gold에서 측정한 CER
2. 반복 운영 비용
3. 목표 동시 회의를 수용하는 rate/concurrency/session limit
4. live speaker diarization 제공 여부와 품질

timestamp는 gate가 아니다. committed transcript가 **utterance-level start/end**를 주면 충족하며, word-level 제공 여부는 동점일 때의 가산 항목으로만 기록한다.

## 현재 코드와 필요한 연결

현재 경로는 `RecordingSession(on_pcm)`이 연결별 `AudioDecoder`를 소유하고, ffmpeg가 Backend의 WebM+Opus/MP4+AAC를 **16 kHz, mono, signed 16-bit little-endian PCM**으로 바꾼 뒤 `on_pcm(bytes)`를 순서대로 `await`한다. 라우터의 `consume_pcm`은 아직 no-op이고 Backend로 전사를 내보내는 schema도 없다.

최소 연결 형태는 다음과 같다.

```text
Backend WebSocket 1개
  └─ RecordingSession 1개
      └─ AudioDecoder 1개
          └─ on_pcm(bytes)
              └─ STT provider WebSocket 1개
                  ├─ partial → normalized interim transcript event
                  └─ committed/final → normalized committed transcript event
```

추가로 필요한 것은 연결별 provider session 수명주기, provider 수신 task, partial/final 정규화, provider 종료·오류·backpressure 처리, 그리고 Backend 반환 schema다. 비교 실험에서는 별도 파일 청킹·재인코딩을 추가하지 않고 각 realtime API의 native finalization을 사용한다. ADR-0001의 회의별 WebSocket, ADR-0004의 realtime service 경계, ADR-0006/0007의 AI-side ffmpeg PCM 출력 결정을 그대로 재사용한다.

## 로컬 측정 근거

### 산출물

- 실험 보고서: `/Users/sungjin/orca/projects/Meety-labeler/docs/stt-api-experiment-report-0831.md`
- 원점수: `/Users/sungjin/orca/projects/Meety-labeler/experiments/6조_0831/scores.json`
- gold 설명과 평가 정책: `/Users/sungjin/orca/projects/Meety-labeler/data/gold/6조_0831/README.md`
- gold manifest: `/Users/sungjin/orca/projects/Meety-labeler/data/gold/6조_0831/manifest.json`
- realtime 상태: `/Users/sungjin/orca/projects/Meety-labeler/experiments/6조_0831/elevenlabs-realtime/status.json`, `/Users/sungjin/orca/projects/Meety-labeler/experiments/6조_0831/gemini-live/status.json`
- 후속 ElevenLabs FILE+VAD 실험: `/Users/sungjin/orca/projects/Meety/research/vad-short-batch-stt-results.md`

저장소 git history와 현재 `/Users/sungjin/wigglewiki`에서는 별도의 gold STT 공급자 비교 결과를 찾지 못했다. Wiki의 `단계 4 멀티스텝 AI 파이프라인 설계.md`에는 ElevenLabs 파일 STT 구상만 있고 점수는 없다.

### 데이터와 지표

원본은 `data/raw/6조_0831.m4a`, mono 한국어 회의 **1,386.858333초(약 23.11분)** 한 개다. gold는 239개 human-verified turn, 6,038 reference characters, 5명 화자이며 70개 turn은 Gemini 기반 초안의 문구를 사람이 수정했다. 이 기원 때문에 Gemini에 유리할 수 있다.

CER은 동일 UEM에서 NFC·소문자화 후 공백·문장부호·기호와 괄호형 웃음 표식을 제거한 문자 편집거리다. DER/JER은 UEM 안에서 provider raw speaker ID를 gold speaker에 최적으로 한 번 대응하고 collar 0, overlap 포함으로 계산했다.

### 정확한 결과

| 공급자 경로 | 평가 범위 | 상태 | CER | DER / JER | 해석 제한 |
| --- | --- | --- | ---: | ---: | --- |
| Gemini 3.5 Transcribe file | 전체 | completed | **5.9622%** (360/6,038) | 70.9774% / 86.6365% | gold 초안 기원 편향 가능 |
| Gemini VAD short-batch | 1,217.1초 부분 | partial | **17.5926%** (969/5,508) | N/A | 실행 자체는 failed, 58개 완료 chunk만 점수화 |
| Gemini 3.5 Transcribe Live | 전체 | completed | **30.9208%** (1,867/6,038) | N/A | word timing이 아닌 근사 event-boundary CER |
| ElevenLabs Scribe v2 file | 전체 | completed | **15.6343%** (944/6,038) | **16.2599% / 33.7782%** | 파일 경로이며 live 품질과 동일하다고 가정할 수 없음 |
| ElevenLabs Scribe v2 Realtime | 첫 600초 | completed | **15.7599%** (449/2,849) | N/A | first-10m UEM, 29개 VAD window; diarization 없음 |
| Speechmatics Enhanced Realtime | 첫 600초 | completed | **15.4791%** | N/A | 동일 realtime gold 조건 정상 완료; 기존 전체 file 결과와 혼용하지 않음 |
| Soniox `stt-rt-v5` clean rerun | 첫 600초 | partial | **16.0056%** (provisional) | N/A | 정확히 600초를 송신했으나 `request_timeout`으로 `finished` 미수신, transcript max end **596.28초**. 정확도는 provisional이고 세션 완료 실패는 안정성 실패로 분리 |
| Deepgram realtime | realtime run (평가 window는 이 노트에 미기록) | completed | **23.6925%** | N/A | 이번 최종 3후보에서 정확도 사유로 제외. 아래 file 결과와 별개 값 |
| Gemini Live (`gemini-3.5-transcribe-live`) | 첫 600초 | completed | **26.7111%** | N/A | 이번 최종 3후보에서 정확도 사유로 제외. 아래 전체 구간 30.9208%와 별개 값 |
| ElevenLabs Scribe v2 VAD+file | 첫 600초 | completed | **14.4261%** (411/2,849) | N/A | realtime이 아니라 29개 파일 호출 |
| Speechmatics Enhanced file | 전체 | completed | **18.9135%** (1,142/6,038) | 37.5714% / 74.0614% | realtime 경로는 미실험 |
| Deepgram Nova-3 file | 전체 | completed | **21.7125%** (1,311/6,038) | 64.6687% / 80.5308% | realtime 경로는 미실험 |
| OpenAI `gpt-4o-transcribe-diarize` | 없음 | blocked | 점수 없음 | 점수 없음 | HTTP 429 `credit_balance_exhausted`, 성능 실패가 아님 |

후속 ElevenLabs FILE+VAD 전체 재실험의 CER은 **14.13%**였지만 gold 발화 종료부터 local consumer 게시까지 p95가 **29.80초**여서 10초 목표를 실패했다. 이는 realtime API 결과가 아니므로 공급자 live 후보의 성능으로 재해석하지 않는다.

## 현재 공급자 주장 비교

아래의 외부 사실은 모두 공급자 1차 문서를 **2026-09-18**에 확인한 값이다. 로컬 측정과 공급자 주장을 섞지 않았다.

| 후보 | 현재 프로젝트 PCM과 Python | 결과 이벤트·한국어·timestamp | 가격 | 동시성·세션·trial caveat | 실제 흐름 적합성 |
| --- | --- | --- | --- | --- | --- |
| **ElevenLabs `scribe_v2_realtime`** | 공식 Python SDK의 server-side realtime 연결을 제공한다. `pcm_16000`은 16-bit little-endian PCM이고 mono만 지원하므로 현재 PCM을 그대로 base64 chunk로 보낼 수 있다. [server-side guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/server-side-streaming), [audio/commit guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies) | `partial_transcript`와 안정된 `committed_transcript`를 내며 `include_timestamps=true`이면 committed word-level timestamp를 제공한다. Korean=`kor`; live diarization은 없다. [event reference](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference), [STT overview/languages](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) | **$0.39/h**, keyterm은 +$0.05/h, 세금 별도. [API pricing](https://elevenlabs.io/pricing/api?price.section=speech_to_text) | realtime 동시성: Free 6, Starter 9, Creator 15, Pro 30, Scale/Business 45, Enterprise elevated. 20개 목표는 Pro 이상이다. session time limit의 숫자는 공개 문서에서 찾지 못했다. 미동의 약관, quota/rate/session-limit 오류 이벤트가 있으며 zero-retention은 Enterprise 또는 trial만 적용된다. [limits](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it), [API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime) | **provisional 후보.** 직결 가능하지만 화자는 사후 단계에서 보강해야 한다. |
| **Speechmatics Enhanced realtime** | 공식 `speechmatics-rt` Python SDK가 `PCM_S16LE`, 16 kHz와 async `send_audio()`를 직접 지원한다. quickstart는 mono raw PCM 예시도 제공한다. [realtime quickstart](https://docs.speechmatics.com/speech-to-text/realtime/quickstart) | `AddPartialTranscript`와 변경되지 않는 `AddTranscript` final을 제공하고 각 word result에 start/end time이 있다. Enhanced의 Korean=`ko`; `diarization: speaker`를 live에서 켤 수 있다. [quickstart](https://docs.speechmatics.com/speech-to-text/realtime/quickstart), [output](https://docs.speechmatics.com/speech-to-text/realtime/output), [languages](https://docs.speechmatics.com/speech-to-text/languages), [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) | Enhanced realtime **$0.43/h**. [pricing](https://www.speechmatics.com/pricing) | Free 2, Pro 50, Enterprise custom concurrent sessions. session은 48h, no-audio 1h, audio/ping 없음 3분에 종료된다. Free는 카드 없이 $100 credit, Pro도 시작 credit $100을 명시한다. [realtime limits](https://docs.speechmatics.com/speech-to-text/realtime/limits), [pricing](https://www.speechmatics.com/pricing) | **provisional 후보.** 현재 PCM과 맞고 live speaker label도 정규화할 수 있다. |
| **Deepgram Nova-3 `language=ko`** | 공식 Python SDK와 Nova-3 Listen v1 WebSocket이 있다. raw headerless 입력은 `encoding=linear16`, `sample_rate=16000`, `channels=1`로 현재 PCM과 맞는다. `linear16`은 signed 16-bit little-endian이다. [Python SDK](https://github.com/deepgram/deepgram-python-sdk), [encoding](https://developers.deepgram.com/docs/encoding), [live guide](https://developers.deepgram.com/docs/live-streaming-audio) | interim 결과는 `is_final=false`, 확정 chunk는 `is_final=true`, 발화 종료는 `speech_final=true`로 구분하며 결과의 각 word에 start/end가 있다. Nova-3는 Korean=`ko`/`ko-KR`와 live diarization을 지원한다. [live results](https://developers.deepgram.com/docs/live-streaming-audio), [endpointing](https://developers.deepgram.com/docs/endpointing), [models/languages](https://developers.deepgram.com/docs/models-languages-overview), [diarization](https://developers.deepgram.com/docs/diarization/) | PAYG monolingual streaming은 현재 한시적 **$0.0048/min($0.288/h)**, regular **$0.0077/min($0.462/h)**; live diarization은 +$0.0020/min(+ $0.12/h). [pricing](https://deepgram.com/pricing) | 공식 rate-limit 표는 Nova-3 streaming starting at 300, diarization streaming up to 100이라고 하지만 pricing 표의 PAYG WSS는 up to 150로 달라 보수적으로 150을 사용한다. $200 free credit, no card/minimum/expiry를 명시한다. Listen v1 최대 session duration은 공개 문서에서 찾지 못했고 idle 연결은 keepalive가 필요하다. [rate limits](https://developers.deepgram.com/reference/api-rate-limits), [pricing](https://deepgram.com/pricing), [connection comparison](https://developers.deepgram.com/docs/flux/flux-nova-3-comparison) | **provisional 후보.** `is_final`과 `speech_final`을 별도 의미로 정규화해야 한다. |
| **Gemini `gemini-3.5-transcribe-live`** | 공식 Google Gen AI Python SDK와 WebSocket을 제공한다. raw 16-bit, 16 kHz, mono, little-endian PCM을 `audio/pcm;rate=16000`으로 보내므로 현재 PCM과 맞는다. [live guide](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe) | `interim_input_transcription`과 확정 `input_transcription`을 제공한다. Korean=`ko-KR`; live에는 word-level timestamp가 없고 utterance-level timestamp만 있다. [live guide](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe), [model/languages](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe) | 공식 추정 blended rate **~$0.009/min(~$0.54/h)**. [pricing](https://ai.google.dev/gemini-api/docs/pricing) | live session 최대 **10분**이라 회의 중 rotation이 필요하다. RPM/TPM/RPD는 project·tier·model별이며 AI Studio의 active limits가 기준이라 고정 concurrency는 공개되지 않았다. Free tier는 무료지만 데이터가 제품 개선에 사용되고, paid tier는 사용되지 않는다고 가격표가 명시한다. [live limits](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [pricing](https://ai.google.dev/gemini-api/docs/pricing) | **screened reference.** word-level timestamp가 필수이면 제외한다. |

OpenAI 경로는 로컬 점수가 없고 지속 오디오 WebSocket live 후보가 아니므로 현재 구현 비교에서 제외했다. 위 표의 과거 file/realtime 측정은 보존한다. 현재 first-600초 realtime 비교의 최종 3후보는 Speechmatics, ElevenLabs, Soniox이며, Deepgram과 Gemini Live는 정확도 사유로 제외했다.

## 최종 3후보 운영 제약 (공급자 1차 문서, 확인일 2026-09-20)

아래 값은 로컬 측정과 분리한 **공급자 공식 문서 값**이다. 각 수치 옆에 해당 수치를 명시한 공식 URL과 확인일을 둔다. 문서가 숫자를 공개하지 않으면 **공개 확인 불가**로 적고 추정하지 않는다.

용어: `동시성`은 동시에 열 수 있는 realtime WebSocket session 수, `RPM/새 연결 rate`는 분당 새 transcription 요청 수, `최대 세션`은 한 연결이 유지될 수 있는 상한, `idle 조건`은 오디오가 오지 않을 때 연결이 끊기는 규칙이다. 셋은 서로 다른 값이므로 섞지 않는다.

| 항목 | Speechmatics Enhanced realtime | ElevenLabs `scribe_v2_realtime` | Soniox `stt-rt-v5` |
| --- | --- | --- | --- |
| **동시성** | Free **2**, Pro **50**, Enterprise **Custom** concurrent realtime sessions. [limits](https://docs.speechmatics.com/speech-to-text/realtime/limits) (2026-09-20) | realtime STT concurrent requests: Free **6**, Starter **9**, Creator **15**, Pro **30**, Scale **45**, Business **45**, Enterprise **Elevated**. 같은 표의 비-realtime STT 값(8/12/20/40/60/60)과 다른 열이므로 혼용하지 않는다. [concurrency](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it) (2026-09-20) | **10** concurrent requests(동시 활성 WebSocket 연결). [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas) (2026-09-20) |
| **RPM / 새 연결 rate** | 동시 session 수와 별개인 **새 연결 RPM·API request cap은 공개 확인 불가**. 공식 limits 문서에 해당 수치가 없다. [limits](https://docs.speechmatics.com/speech-to-text/realtime/limits) (2026-09-20) | 고정 RPM 수치 **공개 확인 불가**. event reference는 `rate_limited`, `commit_throttled` 오류 type만 정의하고 임계값을 공개하지 않는다. [event reference](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference) (2026-09-20) | **100** requests/min. 초과 시 rate limiting. [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas) (2026-09-20) |
| **최대 세션** | **48시간**. [limits](https://docs.speechmatics.com/speech-to-text/realtime/limits) (2026-09-20) | 수치 **공개 확인 불가**. `session_time_limit_exceeded`("Maximum session time has been reached") 오류만 정의한다. [event reference](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference) (2026-09-20) | stream duration **300분**(5시간), **고정값이며 증액 불가**. 초과 시 HTTP **413** `max_duration_reached`로 연결 종료. [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas), [errors](https://soniox.com/docs/api-reference/errors) (2026-09-20) |
| **idle / 무음 종료 조건** | `AddAudio` 없음 **1시간**, audio와 ping/pong 모두 없음 **3분**이면 자동 종료(WebSocket close code **1008**). [limits](https://docs.speechmatics.com/speech-to-text/realtime/limits) (2026-09-20) | 임계 초 **공개 확인 불가**. `insufficient_audio_activity`("You haven't sent enough audio activity to maintain the connection") 오류만 정의한다. [event reference](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference) (2026-09-20) | HTTP **408** `request_timeout`: "The client failed to send audio chunks fast enough. For example, the initial chunk never arrived, or the decoder starved mid-stream." 정확한 idle 초 값은 **공개 확인 불가**("a few seconds" 서술만 있음). [errors](https://soniox.com/docs/api-reference/errors) (2026-09-20) |
| **증액 경로** | 동시 session 상향은 **Support 문의**. [limits](https://docs.speechmatics.com/speech-to-text/realtime/limits) (2026-09-20) | 상향은 **Enterprise Department 문의**로 tailor-made plan 협의. [concurrency](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it) (2026-09-20) | concurrency와 RPM은 **Soniox Console에서 상향 요청 가능**, stream duration만 불가. 초과 시 HTTP **429** `limit_exceeded`이며 organization limit을 먼저 검사한 뒤 project limit을 검사한다. [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas), [concurrency](https://soniox.com/docs/guides/concurrency-limits), [errors](https://soniox.com/docs/api-reference/errors) (2026-09-20) |
| **프로젝트 영향 (잠정)** | 목표 20 동시 회의는 Pro 공개 한도 **50** 안에 든다. 48시간·1시간 여유로 회의 길이 rotation이 필요 없다. 다만 녹음 일시 정지 구간이 3분(ping 포함 완전 무음) 규칙에 걸리는지 확인이 필요하다. | 목표 20 동시 회의는 **Pro(30) 이상**이 필요하다. 최대 세션·idle 숫자가 공개되지 않아 장시간 회의와 무음 구간의 안전성은 **계정 실험으로만** 확인 가능하다. | 기본 공개값 **10 concurrent로는 20 동시 회의를 수용할 수 없어 사전 증액이 필수**다. 100 RPM은 회의 시작 빈도 기준으로 여유가 있고 300분은 회의 길이에 충분하다. 로컬 rerun의 `request_timeout`이 공식 원인(느린 전송·decoder starve)에 해당하는지 재현 확인이 남아 있다. |

모델 기준: Soniox의 현재 active realtime 모델은 `stt-rt-v5`이며 `stt-rt-v4`는 v5로 alias된다. [models](https://soniox.com/docs/stt/models) (2026-09-20)

### 혼동하지 말아야 할 값

- **ElevenLabs client single-use token 15분 만료**는 브라우저 client-side 연결용 임시 토큰의 수명이며("A single use token automatically expires after 15 minutes."), server-side API key 세션의 최대 길이와 **다른 값**이다. [client-side streaming](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/client-side-streaming) (2026-09-20)
- **ElevenLabs 약 36초 자동 commit**("the model automatically commits after approximately 36 seconds of accumulated audio")과 **20~30초 권장 commit 주기**("Committing every 20-30 seconds is good practice to improve latency")는 transcript commit 전략이며 **session limit이 아니다**. [commit strategies](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies) (2026-09-20)
- **Soniox stream duration의 기준 경로**는 `/docs/stt/rt/limits-and-quotas`의 **300분**이다. 구 `core-concepts` 계열 real-time transcription 페이지와 현행 `/docs/stt/rt/real-time-transcription` 페이지에는 duration 수치가 없어, duration을 명시한 문서 경로는 limits-and-quotas 하나뿐이다. 다른 경로에서 본 더 짧은 duration 값은 최신 문서로 대체한다. [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas) (2026-09-20)

### 로컬 계정 측정 (공식 문서 값 아님)

공식 문서의 plan 기본값과 달리, 아래는 **현재 사용 중인 계정에 실제로 적용된 값**의 관측치다. 공식 값과 섞지 않는다.

| 공급자 | 관측값 | 출처 | 해석 |
| --- | --- | --- | --- |
| Speechmatics | realtime 응답 `provider_info`에서 `quota=2`, `usage=1` | 이번 first-600초 realtime run 응답 | 현재 키에 적용된 동시성은 **2**로, 공식 Free 등급 값과 일치한다. Pro의 50은 현재 계정에 적용되어 있지 않다. |
| Soniox | `GET /v1/concurrency-limits` 조회 결과 project `transcribe_concurrent` limit=**null**(미설정), organization `transcribe_concurrent` limit=**10**, current=**0** | 이번 조사 중 실제 API 조회 | 실제 적용 cap은 organization의 **10**이며 공식 공개 기본값과 일치한다. project cap은 설정되어 있지 않다. |
| ElevenLabs | 계정 적용 concurrency·session 값 미조회 | — | 공식 plan 표 외의 실제 적용값은 아직 확인하지 않았다. |

가격은 기존 표의 범위를 유지한다. 운영 제약은 정확도가 실질적으로 구분되지 않을 때만 비용 다음 판단 항목으로 사용하며, 이 절만으로 최종 공급자를 확정하지 않는다.

## 최종 3후보 custom vocabulary · 화자분리 비교 (공급자 1차 문서, 확인일 2026-09-20)

아래도 공급자 공식 문서 값이며 수치마다 URL과 확인일을 둔다. 공개되지 않은 값은 **공개 확인 불가**로 적고 추정하지 않는다.

### Custom vocabulary (도메인 용어 주입)

| 항목 | Speechmatics Enhanced realtime | ElevenLabs `scribe_v2_realtime` | Soniox `stt-rt-v5` |
| --- | --- | --- | --- |
| **기능·필드** | Custom Dictionary의 `additional_vocab`. "Available in: Batch, Realtime, Agent STT"로 realtime 지원이 명시된다. [custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary) (2026-09-20) | keyterm prompting의 `keyterms` 배열("List of keyterms the model is biased towards."). Scribe v2, Scribe v2 Medical(batch), Scribe v2 Realtime에서 지원한다. [realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime), [keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting) (2026-09-20) | `context` 객체. `general`(key-value 배열), `text`(자유형 배경 텍스트), `terms`(용어 문자열 배열), `translation_terms`를 받으며 realtime·async 모두 지원한다. [context](https://soniox.com/docs/stt/concepts/context) (2026-09-20) |
| **규모 한도** | SaaS on Cloud는 custom dictionary가 **20,000 items**를 넘는 job을 거부하고, 권장값은 job당 **1,000 words/phrases 이하**다. **6단어 초과 항목**과 **4,000자 초과 단어**는 transcription 시작 전 자동으로 drop된다. [custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary) (2026-09-20) | realtime은 **최대 50 keyterms, keyterm당 20자**다(batch는 1,000 keyterms / 50자로 별개 값이므로 혼용하지 않는다). [keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting) (2026-09-20) | **최대 8,000 tokens(약 10,000자)**이며 초과하면 오류를 반환한다. [context](https://soniox.com/docs/stt/concepts/context) (2026-09-20) |
| **발음 힌트** | `sounds_like`로 대체 발음을 지정할 수 있다. 단 "sounds_like only works with the main script for that language"다. [custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary) (2026-09-20) | 전용 발음 필드 **공개 확인 불가**. keyterm은 주변 audio 문맥으로 적용 여부를 판단하는 context-aware bias로 설명된다. [keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting) (2026-09-20) | 전용 발음 필드 **공개 확인 불가**. 대신 `text`에 자유형 배경 텍스트를 넣어 문맥을 줄 수 있다. [context](https://soniox.com/docs/stt/concepts/context) (2026-09-20) |
| **추가 비용** | 공식 custom dictionary 문서에 별도 과금 언급 **없음**. 기능별 surcharge는 pricing 페이지에서 **공개 확인 불가**. [custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary) (2026-09-20) | **추가 비용 있음**: "comes at an additional cost"이며 API pricing의 keyterm 항목은 **$0.050/h**다. realtime $0.39/h 기준 keyterm 사용 시 **약 $0.44/h**가 된다. [keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting), [API pricing](https://elevenlabs.io/pricing/api?price.section=speech_to_text) (2026-09-20) | **추가 비용 없음**: "Speaker diarization, language identification, and smart formatting are bundled into the hourly rate."로 $0.12/h에 포함된다. [pricing](https://soniox.com/pricing) (2026-09-20) |
| **한국어 적용** | 한국어 `ko`는 Enhanced 지원 언어지만, 공식 언어표에 custom dictionary의 언어별 지원 열이 없어 **한국어 조합 보장은 공개 확인 불가**다. [languages](https://docs.speechmatics.com/speech-to-text/languages), [custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary) (2026-09-20) | 문서에 언어 제한 서술 **없음**. 한국어 고유명사 적용 품질은 **공개 확인 불가**. [keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting) (2026-09-20) | 문서에 언어 제한 서술 **없음**. [context](https://soniox.com/docs/stt/concepts/context) (2026-09-20) |

### 화자분리 (live speaker diarization)

| 항목 | Speechmatics Enhanced realtime | ElevenLabs `scribe_v2_realtime` | Soniox `stt-rt-v5` |
| --- | --- | --- | --- |
| **realtime 지원** | **지원**. transcription config에 `"diarization": "speaker"`를 설정한다. [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) (2026-09-20) | **미지원**. realtime WebSocket의 문서화된 파라미터에 diarize/speaker 항목이 없고, 기능 목록의 speaker diarization은 batch Scribe v2에만 있다. [realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime), [STT overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) (2026-09-20) | **지원**. `"enable_speaker_diarization": true`. [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization) (2026-09-20) |
| **출력 단위** | 각 word·punctuation 객체의 `speaker` 속성. 라벨은 `S#`(순번) 또는 미식별 시 `UU`. [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) (2026-09-20) | 해당 없음(realtime 출력에 speaker 필드 없음). [realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime) (2026-09-20) | 각 token의 `speaker` 필드(예: `{"text": "How", "speaker": "1"}`). [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization) (2026-09-20) |
| **최대 화자** | `max_speakers`는 **2 이상 정수**, 기본값은 상한 없음. [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) (2026-09-20) | batch Scribe v2 기준 "Speaker diarization, up to 32 speakers"이며 realtime에는 해당 없음. [STT overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) (2026-09-20) | 세션당 **최대 15명**. 목소리가 비슷하면 정확도가 떨어질 수 있다고 명시한다. [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization) (2026-09-20) |
| **튜닝 파라미터** | `speaker_sensitivity`(0–1, 기본 **0.5**, 높을수록 고유 화자 수가 늘어날 가능성 증가), `prefer_current_speaker`(bool, 기본 **false**). [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) (2026-09-20) | 해당 없음. | 공개 확인 불가(문서에 sensitivity류 파라미터 없음). [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization) (2026-09-20) |
| **문서상 품질 caveat** | latency 영향에 대한 서술 **없음**. 한국어 조합 보장도 **공개 확인 불가**. [realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization) (2026-09-20) | 해당 없음. | "Real-time speaker diarization is more challenging due to low-latency constraints"이며 async 대비 "Higher speaker attribution errors"가 있을 수 있다고 명시한다. 전 지원 언어에서 사용 가능하다. [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization) (2026-09-20) |
| **추가 비용** | 기능별 surcharge **공개 확인 불가**(pricing 페이지에 diarization 별도 항목 없음). | 해당 없음. | **포함**(시간당 요금에 bundled). [pricing](https://soniox.com/pricing) (2026-09-20) |

### 프로젝트 영향 (잠정 해석, 결정 아님)

- **화자분리가 decision driver에 들어간 결과 ElevenLabs가 구조적으로 불리해졌다.** realtime 경로에 speaker 필드가 없으므로 CONTEXT.md의 화자·화자 구간·전사 화자 연결을 채우려면 별도 diarization 단계를 붙여야 하고, 이는 ADR-0004의 realtime service 경계에 컴포넌트 하나를 추가하는 비용이다. 로컬 first-600초 realtime 측정에 ElevenLabs DER/JER이 없는 것도 같은 이유다.
- **Speechmatics와 Soniox는 단일 realtime 연결로 전사와 화자 라벨을 함께 받는다.** 다만 두 경로의 출력 단위가 다르다(Speechmatics는 word/punctuation, Soniox는 token). 전사 구간에 대표 화자 한 명을 붙이는 정규화 규칙은 어느 쪽을 택해도 필요하다.
- **custom vocabulary 규모는 ElevenLabs가 가장 좁다.** realtime 50개·20자 제한은 한국어 회의의 제품명·팀명·인명 목록으로는 빠듯하고, 20자 제한에 걸리는 긴 고유명사가 생길 수 있다. Speechmatics 권장 1,000개와 Soniox 약 10,000자는 여유가 있다.
- **비용 순서가 뒤집히는 지점이 있다.** keyterm을 켠 ElevenLabs는 약 $0.44/h로 Speechmatics Enhanced realtime $0.43/h를 넘어선다. Soniox는 $0.12/h에 diarization·context가 모두 포함되어 비용 축에서는 가장 유리하나, 기본 10 concurrent 증액과 `request_timeout` 재현 확인이 선행 조건이다.
- 세 후보 모두 **한국어에서의 custom vocabulary·diarization 실제 품질은 공식 문서로 확인되지 않는다.** 아래 실험 계획에서 동일 gold로 측정해야 한다.

## 제안: 동일 조건 realtime 비교 실험

ADR 결정 전에 Speechmatics, ElevenLabs, Soniox의 기존 first-600초 결과를 완료·재현성까지 포함해 다음 조건으로 재검증한다.

| 항목 | 고정 조건·기록값 |
| --- | --- |
| 입력 | 현재 gold와 같은 원본의 첫 **600초**, **16 kHz mono pcm_s16le**, **1x pacing** |
| finalization | 각 provider의 native finalization 설정과 값을 그대로 기록하고, 임의 공통 VAD로 덮어쓰지 않음 |
| 정확도 | 동일 first-10m UEM의 **CER를 primary metric**으로 사용 |
| timestamp | text-aligned matched word의 timestamp 제공률, start error p50/p95, end error p50/p95 |
| realtime | source word/발화 종료부터 provider final 수신까지 finalization latency p50/p95 |
| 화자분리 | live speaker label을 켠 상태의 **DER/JER**을 기존 gold 평가 정책과 동일 조건으로 측정. Speechmatics는 `diarization: speaker`, Soniox는 `enable_speaker_diarization`. ElevenLabs realtime은 speaker 출력이 없으므로 **미측정**으로 남기고 사후 diarization 단계의 추가 비용·지연을 별도 기록 |
| custom vocabulary | 동일한 회의 도메인 용어 목록으로 on/off 두 run을 돌려 **해당 용어 구간의 CER 변화**를 측정. 각 공급자 한도(Speechmatics 권장 1,000개, ElevenLabs 50개×20자, Soniox 약 10,000자)에 맞춰 목록을 잘라낸 방식과 잘린 항목을 기록 |
| 안정성 | disconnect, provider error, 누락된 final, 재연결 여부와 횟수 |
| 비용 | provider usage 응답·dashboard의 billed audio/usage와 적용 plan 보존 |

Deepgram과 Gemini Live의 기존 측정·screened evidence는 보존하되, 이번 최종 3후보 비교에는 넣지 않는다. Gemini Live는 word-level timestamp가 다시 필수가 될 경우에도 후보 복귀 대상이 아니다.

실험 뒤 위 decision driver 순서로 결과를 비교한다. 정확도 차이가 실질적으로 구분되지 않을 때만 timestamp 품질, 비용, 한도를 차례로 판단하며, 그 전에는 어느 공급자도 추천 또는 ADR 결정으로 표기하지 않는다.

## 미해결 검증

- Speechmatics·ElevenLabs의 first-600초 realtime CER은 정상 완료했지만, 동일 조건을 반복해 CER·finalization latency·billed usage·reconnect를 측정해야 한다. Soniox는 16.0056%(provisional) 뒤 `request_timeout`/`finished` 미수신을 재현·원인 분리·완료 응답 수신까지 검증해야 하며, 그전까지 정확도 순위와 안정성 판정을 함께 묶지 않는다.
- Gemini 기원 편향이 없는 독립 Korean meeting gold가 필요하다. 최소 두 녹음에서 원거리·겹침·잡음 조건을 나눠야 한다.
- 세 후보의 실제 계정에서 20 concurrent sessions, 새 연결 RPM/throughput, maximum session duration, pause/resume 중 idle 유지, rotation/reconnect 후 누락·중복을 확인해야 한다. 공식 문서만으로는 다음이 남는다.
  - Speechmatics: 새 연결 RPM 수치가 공개되지 않는다. 현재 계정은 `quota=2`로 관측됐으므로 20 동시 회의는 Pro 전환 또는 Support 증액이 전제다.
  - ElevenLabs: 최대 session duration·idle 임계 초·RPM 수치가 모두 공개되지 않는다. 지원 문의 또는 계정 실험값으로만 채울 수 있다.
  - Soniox: 기본 10 concurrent / 100 RPM은 공개돼 있고 현재 organization 적용값도 10으로 확인됐다. 20 동시 회의를 위한 Console 증액 요청 결과와 `request_timeout`의 정확한 idle 초 값이 남은 항목이다.
- 화자분리와 custom vocabulary의 공식 문서 비교는 「최종 3후보 custom vocabulary · 화자분리 비교」 절에 정리했다. 남은 검증은 다음과 같다.
  - 세 경로의 realtime DER/JER을 동일 gold 조건에서 측정하지 않았다. ElevenLabs realtime은 speaker 출력이 없어 사후 diarization 단계를 붙였을 때의 추가 지연·비용을 따로 산정해야 한다.
  - Speechmatics `speaker_sensitivity`/`prefer_current_speaker` 기본값(0.5 / false)이 5인 한국어 회의에 적절한지, Soniox의 세션당 15명 상한과 realtime attribution error 경고가 실제로 어느 정도인지 측정하지 않았다.
  - 세 공급자 모두 custom vocabulary의 **한국어 지원 여부·품질이 공식 문서에 없다**. 특히 ElevenLabs realtime의 keyterm당 20자 제한이 한국어 고유명사에 실제로 걸리는지 확인해야 한다.
  - Speechmatics는 custom dictionary·diarization의 기능별 과금 여부가 공개 확인 불가여서 실제 청구서로 확인해야 한다. ElevenLabs keyterm +$0.05/h를 반영하면 약 $0.44/h로 Speechmatics $0.43/h를 넘으므로 비용 비교 시 기능 on/off 조건을 맞춰야 한다.
- `PCM 생성 시각 → Backend committed event` p50/p95/p99와 10초 deadline miss를 측정해야 한다. 기존 realtime의 commit-to-final 610ms p95는 전체 E2E가 아니다.
- provider partial/final을 담을 Backend schema, timestamp 기준, reconnect 시 transcript deduplication 계약이 아직 없다.
- 개인정보 보존·logging/zero-retention 요건과 실제 구독 조건을 Backend·운영 요구와 대조해야 한다.

## 추가 후보 스크리닝

아래는 2026-09-18 기준 공식 문서만으로 한 재스크리닝이다. `통과`는 한국어 realtime, 변경되지 않는 final 결과, **단어별** start/end가 모두 문서에 확인된 경우이고, 아직 gold realtime 정확도를 뜻하지 않는다. 현재 Backend의 압축 청크를 API에 그대로 넣을 수 있다는 뜻도 아니며, 별도 표기가 없으면 기존 `ffmpeg → 16 kHz mono pcm_s16le` 경로를 쓴다.

| 후보 | 판정 | 필수 게이트와 현재 PCM 적합성 | 운영 확인값 |
| --- | --- | --- | --- |
| **AWS Transcribe Streaming** | **통과** | `ko-KR`은 streaming 지원이다. `IsPartial=false`가 완료 segment이고 각 `Items` word에 `StartTime`/`EndTime`이 있다. SDK/HTTP2/WebSocket 경로가 있으며 raw signed 16-bit little-endian PCM을 지원해 현재 PCM과 맞는다. [언어](https://docs.aws.amazon.com/transcribe/latest/dg/supported-languages.html), [partial/final·word time](https://docs.aws.amazon.com/transcribe/latest/dg/streaming-partial-results.html), [입력·연결 방식](https://docs.aws.amazon.com/transcribe/latest/dg/streaming.html) | Boto3는 streaming을 지원하지 않지만, 공식 async Python Streaming SDK와 코드 예제가 있다. Ogg Opus와 PCM은 지원하지만 WebM Opus/fragmented MP4 AAC 직결은 지원 목록에 없어 **비호환**으로 본다. US East 예시는 $0.01/min=$0.60/h이며 리전·tier별 변동한다. HTTP/2+WebSocket 동시 stream 기본은 지원 리전당 **25**이고 quota increase가 가능하다; 최대 session 시간의 숫자는 **공개 확인 불가**다. **live diarization 가능**: `ShowSpeakerLabel`을 켜면 item의 `Speaker`와 speaker-label segment/utterance가 나온다(한국어 locale별 예외는 공개 확인 불가). [Python SDK](https://docs.aws.amazon.com/transcribe/latest/dg/getting-started-sdk.html), [quota](https://docs.aws.amazon.com/general/latest/gr/transcribe.html), [diarization](https://docs.aws.amazon.com/transcribe/latest/dg/diarization.html), [stream item](https://docs.aws.amazon.com/transcribe/latest/APIReference/API_streaming_Item.html), [가격](https://aws.amazon.com/transcribe/pricing) |
| **Google Cloud Speech-to-Text V1 StreamingRecognize** | **통과** | V1 지원 언어표에 `ko-KR`이 있고, `is_final=true` 결과와 `enableWordTimeOffsets`, Python `streaming_recognize` 예제가 있다. `LINEAR16`은 headerless signed 16-bit little-endian PCM이므로 현재 PCM과 맞는다. [V1 Korean](https://cloud.google.com/speech-to-text/docs/v1/speech-to-text-supported-languages), [V1 stream/Python/final](https://cloud.google.com/speech-to-text/docs/v1/transcribe-streaming-audio), [word offsets](https://cloud.google.com/speech-to-text/docs/v1/reference/rest/v1/RecognitionConfig) | V1 price는 data logging $0.016/min 또는 no logging $0.024/min ($0.96/$1.44h)이고 60분 free tier가 있다. 5분 stream, region당 300 concurrent session, 전체 3,000 streaming requests/min이 공개돼 있다. `enableSpeakerDiarization` API 기능은 있지만, 공식 V1 언어표의 `ko-KR` Speaker diarization 열이 비어 있으므로 **Korean live diarization 미지원**이다. `WEBM_OPUS`/`MP4_AAC` 인코딩은 문서상 존재하지만 MediaRecorder 연속 fragment 직결은 **공개 확인 불가**다. [V1 Korean feature table](https://cloud.google.com/speech-to-text/docs/v1/speech-to-text-supported-languages), [diarization API](https://cloud.google.com/speech-to-text/docs/v1/reference/rest/v1/RecognitionConfig), [가격](https://cloud.google.com/speech-to-text/pricing), [quota](https://cloud.google.com/speech-to-text/docs/quotas) |
| **Azure AI Speech realtime** | **통과** | 공식 realtime locale 표에 `ko-KR`이 있고, `Recognizing`(interim)·`Recognized` final 및 `RequestWordLevelTimestamps()` 후 final 상세 결과의 word offset/duration을 제공한다. Python SDK `PushAudioInputStream`과 default 16 kHz/16-bit/mono PCM이 현재 PCM과 맞는다. [Korean realtime locale](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support), [final·word timing](https://learn.microsoft.com/ko-kr/azure/ai-services/speech-service/get-speech-recognition-results), [Python audio input](https://learn.microsoft.com/ko-kr/python/api/azure-cognitiveservices-speech/azure.cognitiveservices.speech.audio?view=azure-python) | 실시간 STT 가격은 지역·통화 선택형 표라 이 시점의 단일 공개 USD 값은 **공개 확인 불가**다. Free 1 / Standard 기본 100 concurrent realtime requests(조정 가능), realtime diarization 최대 240분/session은 확인된다. **live diarization 가능**: `ConversationTranscriber`의 interim/final event가 phrase/event-level `SpeakerId`를 낸다(한국어 조합의 별도 보장은 **공개 확인 불가**). raw PCM 외 WebM Opus/fragmented MP4 AAC의 PushAudioInputStream 직결은 **공개 확인 불가**다. [diarization](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/configure-language-identification-diarization), [quota](https://learn.microsoft.com/ko-kr/azure/ai-services/speech-service/speech-services-quotas-and-limits) |
| **AssemblyAI Whisper Streaming (`whisper-rt`)** | **통과** | 공식 Whisper streaming 지원 언어에 Korean이 있고 `words[]`의 start/end와 `word_is_final=true`, `end_of_turn`을 제공한다. Python 예제는 websocket 클라이언트 경로를 제공하며 `sample_rate=16000` raw audio를 전송할 수 있어 현재 PCM을 붙일 수 있다. [Korean·final word timestamp·3h auto-close](https://www.assemblyai.com/docs/universal-streaming/multilingual-transcription), [Python streaming example](https://www.assemblyai.com/docs/streaming/guides/real_time_translation) | **통과.** 공개 streaming 가격은 $0.15/h, 연결을 열어 둔 전체 session 시간으로 과금한다. free는 5 new streams/min, PAYG는 100+ new streams/min에서 자동 확장하고 concurrent streams는 제한하지 않는다고 한다. **live diarization 가능**: `speaker_labels=true`이면 Turn의 dominant `speaker_label`과 final `words[].speaker`를 제공하며 `whisper-rt`도 지원 모델이다. WebM Opus/fragmented MP4 AAC 연속 청크 직결은 **공개 확인 불가**다. [diarization](https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels), [가격·concurrency](https://www.assemblyai.com/pricing/) |
| **Soniox realtime (`stt-rt-v5`)** | **보류 (screened reference)** | Korean을 포함한 60+ 언어 realtime, `is_final`, `pcm_s16le` 16 kHz mono와 Python SDK는 확인된다. 다만 timing은 “word **or sub-word** token” 단위라 단어별 start/end라는 현재 필수 게이트를 보장하지 않는다. 따라서 제외하지 않고 Gemini와 같이 timestamp 요구가 달라질 때 재검토할 screened reference로 남긴다. [realtime/final/audio](https://soniox.com/docs/stt/rt/real-time-transcription), [timestamp granularity](https://soniox.com/docs/stt/concepts/timestamps), [Python SDK](https://soniox.com/docs/sdk/python-SDK) | $0.12/h이다. **live diarization 가능**: `enable_speaker_diarization=true`이면 token마다 `speaker`가 붙고 전 언어를 지원한다고 한다. 동시성·RPM·stream duration의 공개 기본값은 2026-09-20 현재 [limits & quotas](https://soniox.com/docs/stt/rt/limits-and-quotas)에 공개되어 있다(10 concurrent / 100 RPM / 300분). 위 「최종 3후보 운영 제약」 절을 기준으로 삼는다. `webm`/`aac` container는 받지만 MediaRecorder fragment 직결은 **공개 확인 불가**다. [diarization](https://soniox.com/docs/stt/concepts/speaker-diarization), [가격](https://soniox.com/pricing), [concurrency](https://soniox.com/docs/guides/concurrency-limits), [Python 형식](https://soniox.com/docs/sdk/python-SDK/Full-SDK-reference/types) |
| **Gladia Live** | **보류** | live init은 16 kHz mono 16-bit `wav/pcm`, partial/final 및 word timestamps 옵션을 문서화한다. 하지만 최신 공식 문서에서 Korean live 지원과 해당 word start/end 응답을 함께 확인하지 못했으므로 필수 게이트를 통과로 표기하지 않는다. [live init](https://docs.gladia.io/api-reference/v2/live/init), [공식 live response reference](https://gladia.readme.io/reference/live-audio) | Python SDK 경로는 quickstart에 있다. **live diarization은 공개 확인 불가**: pricing의 generic diarization 표기만으로 live output 단위를 확정할 수 없고 live API response에도 speaker field를 확인하지 못했다. 공개 가격은 Starter $0.75/h, live concurrency는 Free 1/Paid 30, session 최대 3h다. WebM Opus/fragmented MP4 AAC 직결은 **공개 확인 불가**다. [quickstart](https://docs.gladia.io/chapters/live-stt/quickstart), [가격](https://www.gladia.io/pricing), [limits](https://docs.gladia.io/chapters/limits-and-specifications/concurrency) |
| **NAVER CLOVA Speech Streaming** | **보류 (screened reference)** | gRPC realtime API는 `ko`, headerless 16 kHz mono 16-bit PCM, Python protoc 생성 경로를 지원한다. 결과에는 finalization 관련 `epFlag`/`seqId`와 음절(`alignInfos`)별 start/end가 있으나, 문서상 단어가 아니라 **음절** align이다. 따라서 제외하지 않고 Gemini와 같이 timestamp 요구가 달라질 때 재검토할 screened reference로 남긴다. [official streaming guide](https://api.ncloud-docs.com/docs/en/ai-application-service-clovaspeech-grpc) | domain 최대 concurrent stub은 active-calls API로 확인하며 예시 `maxCalls=15`, `RESOURCE_EXHAUSTED` 기준도 domain당 15이다. session lifespan은 hour 단위 threshold만 공개돼 정확한 숫자와 가격은 **공개 확인 불가**다. **live diarization은 공개 확인 불가**: realtime config/response에 speaker field가 없고, 확인된 diarization 문서는 local-file API용이다. WebM Opus/fragmented MP4 AAC는 이 PCM-only realtime API와 비호환이다. [live guide](https://api.ncloud-docs.com/docs/en/ai-application-service-clovaspeech-grpc), [file-only diarization](https://api.ncloud-docs.com/docs/en/ai-application-service-clovaspeech-longsentence-local) |

### 동일 gold realtime 실험에 추가 검토할 수 있는 후보 (결정 아님)

- **AWS Transcribe Streaming**: 현재 PCM 직결과 final word time이 확인됐고, 공개 US East 예시가 $0.60/h다. live diarization은 item/segment speaker label을 제공하지만 Korean locale별 예외와 실제 quota는 계정에서 먼저 확인해야 한다.
- **AssemblyAI Whisper Streaming**: Korean과 final `words[]`가 확인됐고 공개 가격이 $0.15/h다. `whisper-rt`는 Turn·final word speaker label도 제공하며 Korean gold 정확도는 아직 확인되지 않았다.
