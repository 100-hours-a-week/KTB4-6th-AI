# Architecture Decision Records

| ADR                                                     | 결정                                            | 상태       |
| ------------------------------------------------------- | --------------------------------------------- | -------- |
| [ADR-001](0001-backend-ai-live-transcript-websocket.md) | Backend–AI 실시간 전사에 WebSocket 사용               | Accepted |
| [ADR-002](0002-backend-ai-chatbot-websocket.md)         | 회의 QnA에 기존 WebSocket 재사용                      | Accepted |
| [ADR-003](0003-backend-ai-analysis-http-200.md)         | Backend worker의 분석 요청에 동기 HTTP 200으로 최종 결과 반환 | Accepted |
| [ADR-004](0004-ai-service-deployment-boundaries.md)     | 실시간 AI·회의 분석 AI·GPU 추론의 실행 및 배포 분리            | Accepted |
| [ADR-005](0005-stt-provider-speechmatics.md)            | 실시간 전사 STT 공급자로 Speechmatics Enhanced realtime 사용 | Proposed |
| [ADR-006](0006-live-audio-decoding-location.md)         | 실시간 녹음 음성을 AI 서버에서 PCM으로 변환                   | Accepted |
| [ADR-007](0007-live-audio-decoder-ffmpeg-subprocess.md) | 실시간 녹음 음성을 ffmpeg 서브프로세스로 디코딩              | Accepted |

