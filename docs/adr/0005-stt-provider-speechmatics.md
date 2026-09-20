# ADR-0005: 실시간 전사 STT 공급자로 Speechmatics Enhanced realtime을 사용한다

## Status

Proposed

---

## Context

실시간 전사 공급자는 전사 결과의 품질·시간 정보·운영 한도와 비용을 함께 결정한다. 공급자 API의 입력 제약은 이후 오디오 처리 방식에도 영향을 주므로, 오디오 처리 위치와 도구보다 먼저 선택한다.

```text
STT 공급자 선택 (이 ADR)
  └─ 입력 규격 결정
       └─ 오디오 처리 위치 결정 (ADR-0006)
            └─ 디코더 결정 (ADR-0007)
```

초기 후보는 AWS Transcribe, Google Cloud Speech-to-Text, Azure AI Speech, AssemblyAI, Deepgram, ElevenLabs, Speechmatics, Gemini Live, Soniox, Gladia, NAVER CLOVA Speech, OpenAI였다. 한국어 realtime, 확정 결과, 전사 위치에 쓸 timestamp를 기준으로 다음과 같이 걸렀다.

- **비교 전 제외:** Gladia는 한국어 live 응답 지원을 확인하지 못했고, CLOVA는 음절 단위 시간 정보와 live diarization 제약 때문에 제외했다. OpenAI는 지속 오디오 realtime 경로와 실측 점수가 없어 제외했다.
- **추가 실험 대상으로 보류:** AWS·Google·Azure·AssemblyAI는 기본 기능을 갖췄지만, 같은 Korean meeting gold realtime 비교를 하지 않아 결승 후보에 넣지 않았다.
- **실측 결과로 제외:** Gemini Live와 Deepgram은 같은 realtime 실험에서 CER이 낮아 제외했다.
- **결승 후보 유지:** Speechmatics, ElevenLabs, Soniox는 한국어 realtime 전사와 timestamp 요구를 충족해 최종 비교 대상으로 남겼다. Soniox는 word-level timestamp가 필수 요구가 될 때만 제외한다.

최종 비교는 [STT 공급자 조사](stt-provider-research.md)의 동일 Korean meeting gold realtime run과 공급자 공식 문서(2026-09-20 확인)를 사용한다.

---

## Decision Drivers

- **한국어 회의 전사를 정확하게 만들 수 있는가?** 같은 realtime 입력과 gold에서 CER이 낮아야 이후 요약·질의응답·분석이 잘못된 전사에 기대지 않는다.
- **필요한 기능을 포함한 실제 운영비를 감당할 수 있는가?** 시간당 사용료만 비교하지 않고, 사용할 유료 tier의 월 고정비와 포함 사용량, custom vocabulary와 화자분리의 추가비용까지 함께 본다.
- **확정 전사 구간의 시간 정보를 만들 수 있는가?** 시작·종료 시점을 만들 수 있는 timestamp를 받아야 하며, word-level timestamp는 있으면 좋지만 필수 조건은 아니다.
- **목표 동시 20개 회의를 운영할 수 있는가?** 동시성뿐 아니라 새 연결 rate limit, 최대 세션 시간, idle 종료 조건을 확인해 회의 중 예기치 않은 연결 종료를 줄인다.
- **회의 고유명사를 전사에 반영할 수 있는가?** 제품명·인명·약어를 custom vocabulary로 주입할 수 있어야 하며, 지원 규모와 비용, 한국어에서의 실제 효과는 구분해 본다.
- **live 결과에서 Speaker를 보존할 수 있는가?** provider의 raw speaker label을 받아 기존 전사 구간의 대표 Speaker로 정규화하되, 이를 Participant의 신원으로 취급하지 않는다.

---

## Considered Options

### A. Speechmatics Enhanced realtime

final `AddTranscript`의 word timestamp와 `diarization: speaker`의 raw speaker label을 전사 구간에 정규화한다.

**장점**

- 같은 first-600초 실시간 gold에서 CER \*\*15.4791%\*\*로 세 후보 중 가장 낮았고 정상 완료했다.
- word start/end와 live diarization을 한 WebSocket에서 받는다. `speaker_sensitivity`, `prefer_current_speaker`, `max_speakers`로 화자 분리 동작을 조정할 수 있다.
- [`additional_vocab`](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary)을 realtime에서 지원한다. job당 1,000개 이하 권장, 20,000개 초과 거부이며 `sounds_like` 발음 힌트를 제공한다.
- [Pro는 $100 credit으로 시작하며](https://www.speechmatics.com/pricing), 월 약정 없이 사용량 후불이다. Enhanced realtime은 **$0.43/h**, 동시 realtime session은 **50개**다. 500 h/month 초과 사용량에는 20% 할인, model-training opt-in에는 33% 할인 선택지가 있다.
- [최대 세션 48시간, audio 없음 1시간, audio와 ping/pong 모두 없음 3분](https://docs.speechmatics.com/speech-to-text/realtime/limits)이라는 종료 조건이 공개돼 있다.

**단점**

- 결승 후보 중 기본 시간당 단가는 가장 높다. 500 h/month 이하에서 100시간이면 $43이다.

### B. ElevenLabs `scribe_v2_realtime`

`committed_transcript`와 word timestamp를 받는다. realtime에 화자 필드가 없으므로 화자 정보가 필요하면 별도 diarization 경로를 추가해야 한다.

**장점**

- 동일 조건 CER \*\*15.7599%\*\*로 정상 완료했다.
- word-level timestamp를 제공한다.
- [기본 realtime 사용료는 **$0.39/h**](https://elevenlabs.io/pricing/api?price.section=speech_to_text)다.
- [Creator는 **$22/month**](https://elevenlabs.io/pricing/api?price.section=speech_to_text)이며 realtime STT **56시간**과 동시 **15개**를 제공한다.

**단점**

- realtime diarization이 없다. 화자 정보에는 별도 diarization 단계가 필요하다.
- keyterm은 realtime에서 최대 50개, 항목당 20자이고 **+$0.05/h**다. 켜면 $0.44/h가 되어 Speechmatics보다 비싸다.
- [Creator의 동시성은 15개](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it)라 목표 20개 회의에 부족하다.

### C. Soniox `stt-rt-v5`

final token timestamp와 `enable_speaker_diarization`의 raw speaker label을 정규화한다.

**장점**

- [비용은 **$0.12/h**](https://soniox.com/pricing)이며 diarization·context가 시간당 요금에 포함된다. `context`는 최대 8,000 tokens(약 10,000자)까지 제공한다.
- live diarization과 timestamp를 제공한다.
- [공개 기본 한도는 동시 10, 새 요청 100 RPM, 최대 300분](https://soniox.com/docs/stt/rt/limits-and-quotas)이다. 동시성·RPM 증액은 Console에서 요청할 수 있다.

**단점**

- 기본 동시성 10개는 목표 20개에 부족하다.
- timestamp는 word 또는 sub-word token 단위다. 현재 요구는 충족하지만 word-level이 필요해지면 재검토해야 한다.

---

## Decision

**Speechmatics Enhanced realtime을 실시간 전사 STT 공급자로 사용한다.**

- 한국어 동일 realtime gold에서 가장 낮은 CER을 기록하고 정상 완료했다.
- Soniox는 시간당 비용과 vocabulary 규모에서 우수하지만, 종료 결과가 누락됐고 기본 동시성도 10개다. 정상 완료하고 Pro에서 50개 동시성을 제공하는 Speechmatics를 선택한다.
- ElevenLabs는 기본 단가가 조금 낮지만 live diarization이 없어 구조와 비용이 추가된다. keyterm을 켜면 사용료도 Speechmatics보다 높다.
- Speechmatics Pro는 월 고정 구독료 없이 $0.43/h PAYG로 50개 동시성을 제공한다.

---

## Consequences

### Positive

- 하나의 realtime 연결에서 확정 전사, timestamp, raw speaker label을 받는다.
- 20개 동시 회의는 Pro의 공개 한도 50 안에 있다.
- custom dictionary로 대규모 용어 목록을 전사에 반영할 수 있다.

### Negative

- 사용량이 늘면 Soniox 대비 비용 차이가 커진다.
- word의 raw speaker label은 사람 이름이나 Participant가 아니다. 기존 전사 구간을 유지한 채 대표 Speaker 한 명으로 정규화하는 규칙이 필요하다.

---

## Reconsideration Conditions

- Soniox가 누락 없이 완료되고 20개 동시성을 지원하면 비용과 안정성을 다시 비교한다.
- Speechmatics의 전사 또는 화자분리 품질이 회의 사용에 문제가 되면 대안을 다시 비교한다.
- 목표 동시 회의가 50개를 넘거나 새 연결 RPM 제한이 실제 병목이면 Enterprise 협의 또는 다른 공급자를 재검토한다.
- word-level timestamp가 필수 요구로 바뀌면 Soniox를 제외하고 다시 비교한다.
