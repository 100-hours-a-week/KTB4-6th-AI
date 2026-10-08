# Architecture Decision Records

| ADR                                                     | 결정                                            | 상태       |
| ------------------------------------------------------- | --------------------------------------------- | -------- |
| [ADR-001](0001-backend-ai-live-transcript-websocket.md) | Backend–AI 실시간 전사에 WebSocket 사용               | Accepted |
| [ADR-002](0002-backend-ai-chatbot-websocket.md)         | 회의 QnA에 기존 WebSocket 재사용                      | Accepted |
| [ADR-003](0003-backend-ai-analysis-http-200.md)         | Backend worker의 분석 요청에 동기 HTTP 200으로 최종 결과 반환 | Accepted |
| [ADR-004](0004-ai-service-deployment-boundaries.md)     | 실시간 AI·회의 분석 AI·GPU 추론의 실행 및 배포 분리            | Accepted |
| [ADR-005](0005-stt-provider-speechmatics.md)            | 실시간 전사 STT 공급자로 Speechmatics Enhanced realtime 사용 | Accepted |
| [ADR-006](0006-live-audio-decoding-location.md)         | 실시간 녹음 음성을 AI 서버에서 PCM으로 변환                   | Accepted |
| [ADR-007](0007-live-audio-decoder-ffmpeg-subprocess.md) | 실시간 녹음 음성을 ffmpeg 서브프로세스로 디코딩              | Accepted |
| [ADR-008](0008-live-audio-stream-decoder-and-ai-session-lifetime.md) | 녹음 전체 동안 AI 세션 유지·녹음 스트림마다 디코더 교체 | Accepted |
| [ADR-009](0009-recording-file-storage-location.md) | 브라우저에 녹음 원본 보관·FE에서 병합 및 업로드 | Accepted |
| [ADR-010](0010-backend-ai-chatbot-http-200.md) | 회의 QnA의 Backend–AI 통신을 HTTP 요청·응답으로 전환 | Accepted |
