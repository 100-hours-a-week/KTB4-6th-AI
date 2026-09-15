# Architecture Decision Records

AI 위키의 기존 의사결정 기록을 이 저장소로 복사했다. ADR 본문과 승인 상태는 원문을 유지하며, 파일명과 목록 링크만 로컬 문서 구조에 맞췄다.

| ADR | 결정 | 상태 |
| --- | --- | --- |
| [ADR-001](0001-backend-ai-live-transcript-websocket.md) | Backend–AI 실시간 전사에 WebSocket 사용 | Accepted |
| [ADR-002](0002-backend-ai-chatbot-websocket.md) | 회의 QnA에 기존 WebSocket 재사용 | Accepted |
| [ADR-003](0003-backend-ai-analysis-http-200.md) | Backend worker의 분석 요청에 동기 HTTP 200으로 최종 결과 반환 | Accepted |
| [ADR-004](0004-ai-service-deployment-boundaries.md) | 실시간 AI·회의 분석 AI·GPU 추론의 실행 및 배포 분리 | Accepted |

## 출처

- [원본 ADR 목록](https://github.com/100-hours-a-week/KTB4-6th-AI/wiki/Architecture-Decision-Records)
- 복사일: 2026-09-15
- 원본 위키 커밋: `ebcb1458ef52fb24f69ee6610d832239da608399`
- ADR-005는 원본 목록의 빈 자리이며 문서가 없어 복사하지 않았다.
- ADR-004 본문 제목의 `ADR-0004` 표기는 원문 그대로 보존했다.

## 후속 설계 문서와 함께 읽는 기준

[프로젝트 위키](https://github.com/100-hours-a-week/KTB4-6th-wiki/wiki)의 AI 설계 문서 1~7 사이에 충돌이 있으면 높은 번호의 문서를 우선한다. 확인한 설계 위키 커밋은 `f3302de1e9857f4fce387f8423fc824b45b192e5`이다.

- [4. 멀티스텝 AI 파이프라인](https://github.com/100-hours-a-week/KTB4-6th-wiki/wiki/Multistep-AI-Pipeline), [6. 도구·외부 API 통합](https://github.com/100-hours-a-week/KTB4-6th-wiki/wiki/Tool-API-Integration): STT는 Silero VAD로 구간을 나눠 ElevenLabs 파일 API를 호출한다. ADR-001의 외부 Streaming API 그림은 이전 맥락이며 Backend–AI WebSocket 결정과 공급자 호출 방식은 별개다.
- 4번 문서의 초기 음성 에이전트는 기존 확정 전사에서 호출어와 질문을 수집한다. ADR-004에 남아 있는 질문 전용 STT 설명과 차이가 있다. 서비스 분리 결정은 유지하되 별도 질문 STT를 현재 필수 구성으로 해석하지 않는다.
- 4번 문서의 요약 결과는 네 항목의 Markdown이다. 1번 API 문서의 구조화된 요약 항목과 달라 실제 응답 필드 계약의 정합화가 필요하다.
- [5. 데이터·컨텍스트 보강](https://github.com/100-hours-a-week/KTB4-6th-wiki/wiki/Data-Context-Augmentation), 6번 문서: AI는 Backend의 목록·키워드 검색·회의 조회 도구로 근거를 보강한다. Backend DB 직접 접근 금지와 양립하며, 최초 요청 snapshot만 사용하는 설계로 제한하지 않는다.

위 설명은 후속 설계와 기존 ADR의 차이를 안내하는 것으로, 원본 ADR의 결정이나 승인 상태를 변경한 것은 아니다.
