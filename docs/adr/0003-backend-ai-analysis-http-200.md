## ADR-003: 요약, 분석 작업의 Backend-AI 서버 통신에 동기 HTTP 응답 사용

# Status

Accepted

---
## Context
  
Meety는 종료된 회의 전사를 바탕으로 요약, 팀 커뮤니케이션 리포트, 회의 지표를 생성함.
대상 AI 서버 API는 `POST /summary`, `POST /report`, `POST /metric`이다. 이 작업들은 LLM
호출을 포함하므로 요청 완료 시간이 짧다고 보장할 수 없음.

이 ADR의 범위는 **백엔드와 AI 서버 사이의 내부 통신**임. 브라우저·사용자 API는 이 ADR의 범위가 아님.

현재 제약, 요구사항 등의 설계 맥락은 다음과 같음.
- 현재 백엔드 ERD에는 `ai_requests` 도메인이 있으며, `ACCEPTED`, `PROCESSING`, `COMPLETED`, `FAILED` 상태와 멱등 키를 저장한다.
- 결과는 `meeting_summaries`, `analysis_reports`, `meeting_metrics`에 백엔드가 저장한다.
- AI 서버는 백엔드 DB를 직접 조회하거나 요청 결과를 별도로 기록하지 않는다.

검토할 선택지는 다음 두 가지다.
- A. Backend가 AI 서버 HTTP endpoint를 호출하고 완료 결과를 `200 OK`로 받는다.
- B. AI 서버가 HTTP `202 Accepted`로 자체 job을 접수하고, 나중에 callback 또는 polling으로 결과를 전달한다.
---
## Decision Drivers

- **작업 상태와 결과의 진실 원본을 한 서비스에 유지할 수 있는가?** 백엔드가 보유한 작업 상태와 결과를 AI 서버에도 중복 저장해 동기화하는 부담을 줄인다.
- **v1에서 추가 운영 요소를 최소화할 수 있는가?** 별도 queue, callback, polling API와 중복 상태 저장 없이 현재 분석 요청을 처리할 수 있는지 본다.
- **LLM 지연을 사용자 HTTP 요청 수명과 분리할 수 있는가?** 분석이 오래 걸려도 사용자 연결은 먼저 종료하고, 완료 결과는 백엔드가 별도로 관리할 수 있어야 한다.
- **멱등성·재시도·실패 처리와 결과 저장의 책임이 명확한가?** 연결이 끊기거나 작업이 실패했을 때 복구와 저장을 어느 서비스가 맡는지 분명해야 한다.
- **모델 호출 동시성을 제한하고 확장할 수 있는가?** 분석 요청이 늘어날 때 처리량을 제어하고 worker를 확장할 경로가 있어야 한다.

---
## Considered Options

### 1. A: Backend worker가 동기 HTTP `200 OK`까지 대기
백엔드는 `ai_requests`를 생성한 뒤 worker가 작업을 획득한다. worker는 전사 snapshot을 담아 AI 서버의 `/summary`, `/report`, `/metric`을 호출하고, AI 서버가 모델 생성을 끝낸 뒤 `200 OK` 본문으로 결과를 반환한다. worker는 결과를 백엔드 DB에 저장한다.
```text
Backend API → ai_requests = ACCEPTED → 사용자에게 202
Backend worker → ai_requests = PROCESSING
Backend worker ── POST /summary ──► AI Server
Backend worker ◄─ 200 OK + result ── AI Server
Backend worker → 결과 저장 → ai_requests = COMPLETED
```
**장점**
- 백엔드의 기존 `ai_requests`가 유일한 job 상태 저장소가 된다.
- AI 서버는 stateless에 가깝게 모델 호출과 결과 반환만 담당한다.
- callback endpoint, polling endpoint, AI 서버 job DB가 필요 없다.
- 결과 저장, 크레딧, 알림, 재시도 책임이 백엔드에 모여 있다.
- 사용자 요청은 이미 `202`로 분리되므로, AI 응답 대기는 backend worker만 감당한다.

**단점**
- LLM 생성이 길면 worker와 AI 서버 사이의 HTTP 연결도 길게 유지된다.
- internal gateway, HTTP client, AI 서버의 timeout을 작업 시간에 맞춰 운영해야 한다.
- 동시에 많은 분석이 들어오면 backend worker 수와 AI 호출 동시성을 제한해야 한다.
- HTTP 응답을 받기 전 연결 단절 시, 모델 호출 완료 여부가 불명확할 수 있어 멱등 키와 재시도 규칙이 필요하다.
### 2. B: AI 서버가 HTTP `202 Accepted`로 async job을 접수
백엔드는 AI 서버에 분석 요청을 보내고, AI 서버는 자체 queue에 job을 넣은 뒤 `202 Accepted`와 AI 서버 job ID를 반환한다. 완료 결과는 AI 서버가 백엔드 callback endpoint로 전송하거나, 백엔드가 AI 서버의 job 조회 endpoint를 polling한다.
```text

Backend ── POST /summary ──► AI Server

Backend ◄─ 202 Accepted + ai_job_id ── AI Server
AI Server queue/worker → model generation

AI Server ── callback result ──► Backend
또는
Backend ── GET /ai-jobs/{id} ──► AI Server

```
**장점**
- backend worker와 AI 서버 사이의 HTTP 연결을 장시간 유지하지 않는다.
- AI 서버가 모델별 queue, 동시성, rate limit, 우선순위와 retry를 중앙에서 제어할 수 있다.
- 모델 처리 worker를 AI 서버 중심으로 독립 확장하기 쉽다.

**단점**
- AI 서버에 durable queue와 job 상태 저장소가 추가된다.
- callback 방식은 백엔드 수신 endpoint, 서비스 간 인증, 재시도, 중복 event 처리가 필요하다.
- polling 방식은 AI 서버 status API, polling 주기, 상태 보존 기간을 추가해야 한다.
- 백엔드 `ai_requests`와 AI 서버 job 상태가 분리되어 상태 불일치와 복구 규칙이 필요하다.
- AI 서버의 책임이 모델 실행에서 job 플랫폼 운영으로 넓어진다.
---
## Decision
**v1에서 `/summary`, `/report`, `/metric`은 backend worker가 호출하는 동기 HTTP API로 구현하고, AI 서버는 생성 결과를 `200 OK` 응답으로 반환한다.**

사용자에게 보이는 비동기성은 백엔드가 담당한다.
```text

사용자 → Backend: 분석 생성 요청
Backend → 사용자: 202 Accepted + ai_request_id
  
Backend worker → AI Server: POST /summary, /report, /metric
AI Server → Backend worker: 200 OK + 최종 결과
```
선택 이유:
- `ai_requests`와 결과 테이블이 이미 백엔드에 있어 상태의 진실 원본을 중복시키지 않는다.
- AI 서버가 job queue, callback, polling API, 결과 영속화를 구현하지 않아도 된다.
- 분석 시간이 길어도 사용자 HTTP 연결은 `202` 응답 뒤 종료되어 있다.
- v1의 운영 복잡도를 낮추고, AI 서버의 책임을 모델 실행으로 제한한다.
---
## Consequences
### Positive
- 분석 job 상태와 결과 저장의 진실 원본이 백엔드로 단일화된다.
- AI 서버 API가 요청 snapshot을 입력받아 최종 결과를 반환하는 단순한 모델 adapter가 된다.
- AI 서버 callback 수신 endpoint, job polling API, AI 서버 job DB가 필요 없다.
### Negative
- 긴 모델 호출 동안 backend worker와 AI 서버 사이 HTTP 연결이 유지된다.
- timeout, retry, 멱등 키, worker concurrency를 백엔드가 운영해야 한다.
- AI 서버 처리 시간이 매우 길어지거나 요청량이 커지면 backend worker가 병목이 될 수 있다.
- AI 서버가 완료 결과를 멱등하게 재반환하려면 짧은 기간의 idempotency result cache가 필요하다.
---
## Revisit When
- 분석 요청량이 증가해 backend worker pool이 지속적인 병목이 될 때
- AI 서버가 durable queue와 독립 worker fleet을 운영해야 할 만큼 모델·GPU 작업이 늘어날 때
- 분석 작업의 처리 시간이 수분 이상으로 늘어나 internal HTTP timeout 운영이 부적절해질 때
- AI 서버가 중앙 rate limit·우선순위 queue를 직접 소유해야 할 제품 요구가 생길 때
