# ADR-0005-1: 실시간 회의 전사에 Speechmatics Enhanced realtime을 사용한다

## Status

Accepted

---

## Context

Meety는 회의가 진행되는 동안 한국어 음성을 전사하고, 그 기록을 회의 질의응답·요약·분석에 사용한다. 전사 내용뿐 아니라 발언 위치를 찾을 수 있는 시간 정보가 필요하며, 동시에 20개 회의를 처리하는 것을 목표로 한다. 회의에 등장하는 제품명·인명·기술 용어도 전사에 반영할 수 있어야 한다.

이를 위해 한국어 실시간 전사 API를 조사하고 회의 녹음으로 비교했다. 최종 후보를 좁힌 과정은 다음과 같다.

- **Deepgram·Gemini Live:** 전사 실험에서 Speechmatics·ElevenLabs보다 문자 오류율(CER)이 높아 제외했다.
- **AWS Transcribe·Google Cloud Speech-to-Text·Azure AI Speech·AssemblyAI:** 기본 기능은 확인했으나 회의 녹음 비교까지 진행하지 않아 후속 검토 대상으로 남겼다.
- **Gladia·NAVER CLOVA Speech:** 조사에서 한국어 실시간 결과와 화자 정보 등 필요한 기능의 제공 범위를 충분히 확인하지 못해 보류했다.
- **OpenAI:** 조사한 파일 전사 경로는 회의 중 연속 전사와 맞지 않았고, 실험도 크레딧 부족으로 완료하지 못해 제외했다.

최종적으로 **Speechmatics Enhanced realtime, ElevenLabs Scribe v2 Realtime, Soniox**를 비교한다. Speechmatics는 Pro, ElevenLabs는 Creator 요금제를 기준으로 한다.

---

## Decision Drivers

- **한국어 회의 내용을 정확하게 전사할 수 있는가?** 전사 품질은 이후에 이어지는 QnA·요약·분석 성능에 크게 영향을 미치므로, 실제 회의 녹음의 문자 오류율을 우선 비교한다.
- **개발부터 운영까지 비용 부담이 적은가?** 시간당 요금과 함께 무료 크레딧, 월 구독료, 포함 사용량, 기능 추가 요금과 동시성 확대 비용을 비교한다.
- **실시간 전사 timestamp 기능이 존재하는가?** 전사 구간의 시작·종료 시간을 구성할 수 있어야 한다. 단어별 시간 정보까지 제공하면 활용하기 좋지만 필수 조건은 아니다.
- **동시에 20개 회의를 처리할 수 있는가?** 동시 연결 수와 새 연결 요청 제한을 확인하고, 회의 길이와 일시 정지를 감당할 수 있는지도 함께 본다.
- **회의에서 사용하는 고유명사를 반영할 수 있는가?** custom vocabulary로 제품명·인명·약어를 전달할 수 있어야 하며, 등록 가능한 규모와 추가 비용을 비교한다.
- **전사와 함께 화자 정보를 받을 수 있는가?** 실시간 화자분리를 제공하면 회의 중 발언자를 구분하는 데 활용할 수 있다.

---

## Considered Options

### A. Speechmatics Enhanced realtime — Pro

실시간 전사와 단어별 시간 정보, 화자 라벨을 함께 제공한다.

**장점**

- **한국어 전사 성능이 좋다.** 회의 녹음 비교에서 CER은 \*\*15.48%\*\*였고 전사를 정상 완료했다.
- **월 고정비 없이 20개 동시 회의를 수용한다.** Pro는 사용량 기반 요금제로 **$0.43/시간**, 동시 연결 **50개**를 제공한다. 신규 계정에는 **$100 크레딧**이 제공된다. [요금](https://www.speechmatics.com/pricing)
- **장시간 회의를 지원한다.** 최대 세션은 **48시간**이다. 오디오가 없으면 1시간, 오디오와 ping/pong이 모두 없으면 3분 후 종료된다. [운영 한도](https://docs.speechmatics.com/speech-to-text/realtime/limits)
- **회의 용어를 넉넉하게 등록할 수 있다.** custom dictionary는 **1,000개 이하를 권장**하며 발음 힌트도 지원한다. [Custom dictionary](https://docs.speechmatics.com/speech-to-text/features/custom-dictionary)
- **시간 정보와 화자분리를 함께 제공한다.** 별도 호출 없이 단어별 시작·종료 시점과 화자 라벨을 받을 수 있다. [실시간 출력](https://docs.speechmatics.com/speech-to-text/realtime/output), [화자분리](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization)

**단점**

- **기본 사용료가 다른 두 후보보다 높다.** 크레딧과 할인을 제외한 100시간 사용료는 **$43**이다.

### B. ElevenLabs Scribe v2 Realtime — Creator

실시간 전사와 단어별 시간 정보를 제공한다. 화자분리는 실시간 API에 포함되지 않는다.

**장점**

- **한국어 전사 성능이 좋다.** 회의 녹음 비교에서 CER은 \*\*15.76%\*\*였고 전사를 정상 완료했다.
- **월 구독에 전사 사용량이 포함된다.** Creator는 **월 $22**, 첫 달은 **$11**이며, realtime STT **56시간**이 포함된다. 기본 시간당 요금은 **$0.39**다. 포함 시간은 구독에 따른 사용량이며 별도의 무료 크레딧은 아니다. [요금](https://elevenlabs.io/pricing/api?price.section=speech_to_text)
- **단어별 시간 정보와 용어 주입을 지원한다.** keyterm은 최대 **50개**, 항목당 **20자**까지 등록할 수 있다. [Realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime), [Keyterm prompting](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/keyterm-prompting)

**단점**

- **Creator의 동시성으로는 목표를 충족하지 못한다.** 동시 연결은 **15개**다. 20개 회의를 수용하려면 상위 요금제 등 추가 조치가 필요하다. [동시성](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it)
- **용어 주입에 추가 요금이 든다.** keyterm 사용 시 **$0.05/시간**이 추가돼 기본 전사 요금과 합하면 **$0.44/시간**이다. [요금](https://elevenlabs.io/pricing/api?price.section=speech_to_text)
- **실시간 화자분리가 없다.** 회의 중 화자 정보가 필요하면 별도 처리가 필요하다.

### C. Soniox

실시간 전사와 token 단위 시간 정보, 화자 라벨을 제공한다.

**장점**

- **기본 전사 비용이 낮다.** 사용량 기반으로 약 **$0.12/시간**이며 화자분리는 별도 추가 요금 없이 제공한다. custom context는 입력 text token 사용량으로 과금한다. [요금](https://soniox.com/pricing)
- **회의 용어와 배경 설명을 함께 전달할 수 있다.** context에 최대 **8,000 tokens**를 넣을 수 있다. [Context](https://soniox.com/docs/stt/concepts/context)
- **시간 정보와 실시간 화자분리를 제공한다.** token별 시간 정보로 전사 구간의 시작·종료 시점을 구성할 수 있다. [Timestamps](https://soniox.com/docs/stt/concepts/timestamps), [화자분리](https://soniox.com/docs/stt/concepts/speaker-diarization)
- **요청 한도가 명시돼 있다.** 새 요청은 **분당 100개**, 세션 길이는 최대 **300분**이다. [운영 한도](https://soniox.com/docs/stt/rt/limits-and-quotas)

**단점**

- **기본 동시성으로는 목표를 충족하지 못한다.** 동시 연결은 **10개**이며, 20개 회의를 수용하려면 Console에서 한도 상향을 요청해야 한다. [운영 한도](https://soniox.com/docs/stt/rt/limits-and-quotas)
- **전사 실험에서 종료를 정상 완료하지 못했다.** 수신 결과의 CER은 \*\*16.01%\*\*였지만, 종료 과정에서 timeout이 발생했다.

---

## Decision

**Speechmatics Pro의 Enhanced realtime을 사용한다.**

한국어 회의 전사 성능이 좋고, 월 고정 구독료 없이 목표인 20개 동시 회의를 수용한다. 단어별 시간 정보와 실시간 화자분리를 함께 제공하며, 회의 용어를 등록할 수 있는 규모도 충분하다.

ElevenLabs Creator는 전사 성능이 비슷하지만 동시 연결이 15개이고 실시간 화자분리가 없다. Soniox는 비용이 가장 낮지만 동시성 확대가 필요하고, 전사 실험에서 종료 오류가 발생했다. 현재는 낮은 단가보다 필요한 동시성과 기능을 갖추고 정상 완료한 Speechmatics를 우선한다.

---

## Consequences

### Positive

- 별도 동시성 증액 협의 없이 20개 회의를 처리할 수 있다.
- 전사·시간 정보·실시간 화자 라벨을 한 공급자에서 받는다.
- 초기 크레딧으로 개발 비용을 줄이고, 이후에는 사용량에 따라 비용을 지불한다.
- 회의별 고유명사와 기술 용어를 전사에 반영할 수 있다.

### Negative

- 사용 시간이 늘어날수록 Soniox와의 비용 차이가 커진다.
- 공급자의 화자 라벨을 서비스의 전사 구간에 연결하는 처리는 필요하다.

---

## Reconsideration Conditions

- 실제 회의에서 전사 정확도나 용어 인식이 서비스 사용에 지장을 주면 대안을 재검토한다.
- 동시 회의가 50개를 넘거나 요청 제한이 병목이 되면 한도 확대와 공급자 변경을 비교한다.
- 사용량 증가나 요금 변경으로 전사 비용 부담이 커지면 요금제와 공급자를 다시 검토한다.
