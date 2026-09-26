## ADR-002: Backend-AI 실시간 회의 챗봇 통신에 WebSocket 사용

## Status

Accepted

---
## Context

Meety의 회의방 QnA는 한 사용자가 질문하면 챗봇 답변을 현재 회의 참가자 모두가 보고, 나중에 입장한 참가자도 질문과 답변 이력을 확인할 수 있는 기능임.

답변 생성에는 LLM 호출이 필요하므로 즉각적인 응답을 보장할 수 없음.

주요 요구사항과 제약은 다음과 같음
- AI 답변 생성 시간은 짧다고 보장할 수 없다.
- 회의당 활성 질문은 하나다. 백엔드가 동시에 두 질문을 AI 서버에 보내면 안 된다.
- 백엔드와 AI 서버는 이미 실시간 전사용 장기 WebSocket 연결을 사용한다.
- AI 서버는 백엔드 DB를 직접 조회하거나 결과를 영구 저장하지 않는다.

`/chat` 모델 호출 결과를 기다리는 HTTP `200 OK`, AI 서버가 job을 접수하는 HTTP `202 Accepted`, 기존 장기 WebSocket의 요청·응답 이벤트 방식을 검토함.

---
## Decision Drivers

- **긴 LLM 생성 시간을 사용자 요청 수명과 분리할 수 있는가?** 백엔드가 사용자 요청을 받은 연결을 답변 생성이 끝날 때까지 유지하지 않아도 되어야 한다.
- **회의당 하나의 활성 질문을 백엔드가 제어할 수 있는가?** 답변 생성 중에는 다른 질문이 동시에 처리되지 않도록 백엔드가 질문 상태를 일관되게 관리한다.
- **기존 백엔드–AI 서버 WebSocket 연결을 재사용할 수 있는가?** 전사용 연결에서 질문과 답변도 전달해 별도 연결·인증 경로의 추가를 줄인다.
- **연결 단절과 재전송에도 중복 처리를 막을 수 있는가?** 요청과 답변을 식별해 같은 질문의 재전송이나 중복 답변 수신을 일관되게 처리한다.

---
## Considered Options

### 1. 백엔드가 `POST /chat`을 호출하고 HTTP `200 OK`까지 대기

백엔드 worker가 전사 snapshot을 담아 AI 서버 `POST /chat`을 호출하고, AI 서버가 LLM 생성을 끝낸 뒤 답변을 `200 OK` 본문으로 반환한다.

**장점**
- HTTP 요청·응답 모델이 단순하다.
- AI 서버는 별도 job 상태 저장, callback, polling API가 필요 없다.
- 백엔드 worker가 결과를 받은 즉시 DB에 저장할 수 있다.
- 짧고 완료 시간이 예측 가능한 모델 호출에 적합하다.

**단점**
- LLM 완료까지 backend worker의 HTTP 연결을 유지한다.
- AI 처리 시간이 길거나 변동이 크면 internal gateway와 client timeout 정책을 맞춰야 한다.
- 전사 WebSocket과 chat HTTP가 별도 연결·인증·관측 경로가 된다.
- 답변 생성 중 상태와 완료 이벤트를 AI 서버가 backend WebSocket에 전달하려면 별도 연결 연계가 필요하다.
### 2. 백엔드가 `POST /chat`을 호출하고 AI 서버가 HTTP `202 Accepted`로 job 접수

AI 서버가 `ai_request_id`를 job으로 접수해 `202 Accepted`를 반환한다. 완료 결과는 AI 서버가 백엔드 callback endpoint로 push하거나, 백엔드가 AI 서버 job 상태 endpoint를 polling한다.

**장점**
- 긴 모델 작업을 HTTP 연결과 분리한다.
- AI 서버가 작업 queue, 제한, 재시도를 독립적으로 관리할 수 있다.
- 매우 긴 분석 작업에도 같은 패턴을 적용할 수 있다.

**단점**
- `202 Accepted`는 접수만 뜻하므로 결과 전달 수단이 추가로 필요하다.
- callback 방식은 백엔드 수신 endpoint, callback 인증, 재시도, 중복 event 처리를 추가한다.
- polling 방식은 AI 서버 status API와 polling 주기·부하·상태 보존을 추가한다.
- AI 서버가 job 상태를 영구 저장하지 않는 현재 v1 책임 경계와 맞지 않는다.
### 3. 기존 백엔드-AI 서버 WebSocket에서 `chat.request`/`chat.completed` 이벤트 교환

백엔드는 회의별 AI 서버 WebSocket에서 전사 PCM 이벤트와 별도로 `chat.request`를 보낸다. AI 서버는 접수 ack와 LLM 완료 결과를 같은 WebSocket으로 비동기 전송한다.

**장점**
- 이미 존재하는 장기 양방향 연결을 재사용한다.
- HTTP 연결을 LLM 생성 완료까지 유지하지 않는다.
- AI 서버 callback endpoint나 job polling endpoint가 필요 없다.
- `chat.accepted`, `chat.completed`, `chat.failed` 이벤트로 상태 변화를 즉시 백엔드에 전달한다.
- `ai_request_id`와 `client_message_id`를 이벤트에 넣어 재전송·중복 처리를 명확히 할 수 있다.

**단점**
- WebSocket application protocol과 재연결·미전달 결과 복구를 직접 설계해야 한다.
- 전사와 chat 이벤트를 한 연결에 multiplex하므로 요청 ID와 역압 정책이 필요하다.
- WebSocket 자체는 회의당 하나의 질문 정책을 보장하지 않으므로 백엔드 DB 잠금이 필요하다.

---
## Decision

**백엔드와 AI 서버는 기존 회의별 WebSocket을 통해 `chat.request`와 결과 이벤트를 교환한다.**

선택 이유:
- 백엔드-AI 서버 사이에 이미 전사 목적의 장기 양방향 WebSocket이 있으므로, 같은 인증된 연결에서 QnA 요청·진행 상태·완료 결과를 전달할 수 있다.
- LLM 생성이 길어져도 백엔드는 HTTP 요청을 보류하거나 AI 서버를 polling하지 않는다.
- AI 서버가 별도 job DB를 소유하지 않아도 된다.
- 회의당 하나의 질문 정책은 AI 서버 연결 상태가 아니라 백엔드 DB에서 강제한다.

---
## Consequences

### Positive
- 긴 LLM 생성 시간을 HTTP 연결이나 AI 서버 callback과 분리한다.
- AI 서버가 추가 job endpoint, callback delivery, polling 상태 API를 구현하지 않아도 된다.
- 전사와 QnA의 backend-AI 이벤트가 한 인증된 장기 연결에서 흐른다.
- 백엔드는 `ai_requests`와 `ai_messages`를 단일 진실 원본으로 유지한다.
- 회의별 단일 활성 질문을 모델 처리 속도와 무관하게 DB에서 보장한다.
### Negative
- WebSocket event schema, multiplexing, 재연결 등, 긴 비동기 모델 작업을 WebSocket으로 안전하게 처리하려면 연결 단절과 중복 요청을 위한 상태 관리가 추가로 필요하다
- 같은 연결에서 전사 오디오와 LLM 작업이 함께 흐르므로 chat 작업이 오디오 처리 지연을 만들지 않도록 task isolation과 backpressure를 설계해야 한다.
- AI 서버가 상태를 영구 저장하지 않는다면, 긴 작업 중 프로세스 재시작 시 백엔드 재시도와 실패 처리로 복구해야 한다.
- HTTP request/response보다 운영 관측과 테스트가 복잡해진다.
---
## Reconsideration Conditions
- AI 서버가 durable queue와 job 상태 저장소를 운영하게 되어 `202 Accepted` job API가 더 단순해질 때
- 전사 WebSocket과 chat 작업을 분리해야 할 만큼 chat 동시성 또는 모델 부하가 증가할 때
- 결과 token을 실시간 stream하거나, 여러 질문을 회의별 queue로 처리해야 할 때
