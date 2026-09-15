# ADR-001: Backend–AI 실시간 전사 통신에 WebSocket 사용

## Status

Accepted

---

## Context

오프라인 회의에서 대표 참여자 1명이 브라우저를 통해 음성을 녹음하고, 백엔드로 실시간 전송함.

백엔드는 오디오를 AI 서버로 전달하고, AI 서버는 외부 STT API를 통해 전사를 수행함. 생성된 전사 결과는 다시 백엔드를 통해 참여자에게 전달함.

```text
Recorder Browser
      │ WebSocket
      ▼
Backend Server
      │ ?
      ▼
AI Server
      │ Streaming API
      ▼
External STT API
```

주요 요구사항은 다음과 같음.

- 오디오의 지속적인 실시간 전송
- 전사 결과의 비동기 수신
- 발화 단위 전사 제공
- 약 5~10초 수준의 지연 허용
- 초기 단계에서 대규모 동시 전사 세션을 전제로 하지 않음
- AI 서버는 FastAPI 기반으로 구성

Backend–AI Server 간 통신 방식으로 HTTP, WebSocket, gRPC Bidirectional Streaming을 검토함.

---

## Decision Drivers

- 실시간 양방향 스트리밍 지원 여부
- 현재 요구사항 대비 구현 복잡도
- 기존 기술 스택 활용 가능성
- 향후 확장 시 유지보수성

---

## Considered Options

### 1. HTTP

**장점**
- 구현 및 디버깅이 가장 단순함
- 기존 REST API 기술 활용 가능

**단점**
- 연속적인 오디오 스트림과 세션 관리에 부자연스러움
- 전사 결과 실시간 전달을 위해 별도 통신 방식이 필요할 수 있음

### 2. WebSocket

**장점**
- 하나의 연결에서 오디오 전송과 전사 결과 수신 가능
- FastAPI에서 직접 지원하며 기존 WebSocket 경험 재사용 가능

**단점**
- 메시지 schema와 세션·오류·재연결 정책을 직접 정의해야 함
- 규모가 커질수록 자체 protocol 관리 비용이 증가할 수 있음

### 3. gRPC Bidirectional Streaming

**장점**
- `.proto` 기반의 명확한 서비스 계약 제공
- 양방향 스트리밍과 flow control, status, cancellation 등의 기능 제공

**단점**
- gRPC runtime, Protocol Buffers, code generation 등 추가 기술 스택 필요
- 현재 단순한 인터페이스에서는 도입 복잡성 대비 이점이 제한적임

---

## Decision

**Backend–AI Server 간 실시간 전사 통신 방식으로 WebSocket을 사용함.**

선택 이유:

- 현재 통신은 오디오 입력과 전사 이벤트 반환을 중심으로 단순하며 WebSocket으로 충분히 구현 가능함
- FastAPI에서 WebSocket을 직접 지원하므로 기존 애플리케이션 구조를 활용할 수 있음
- Frontend–Backend에서도 WebSocket을 사용하고 있어 팀 내 관련 경험을 재사용할 수 있음
- gRPC의 계약 관리 및 스트리밍 기능은 장점이지만 현재 규모에서는 추가 도입 비용을 정당화할 만큼 필요성이 크지 않음

---

## Consequences

### Positive

- 기존 기술 스택을 활용하여 빠르게 구현 가능
- 추가적인 gRPC 학습 및 환경 구성 비용 감소
- 현재 요구되는 실시간 전사 구조와 latency를 충분히 지원 가능

### Negative

- 메시지 schema 및 protocol 규칙을 직접 관리해야 함
- 재연결, 오류 처리, backpressure 등의 정책을 직접 구현해야 함
- 향후 인터페이스가 복잡해지면 WebSocket protocol 유지보수 비용이 증가할 수 있음

---

## Revisit When

- Backend–AI 간 API와 메시지 종류가 증가하여 protocol 관리 비용이 커지는 경우
- 동시 전사 세션 증가로 connection 및 flow control이 주요 운영 문제가 되는 경우
- AI Server가 여러 독립 서비스로 분리되어 서비스 간 계약 관리가 중요해지는 경우
