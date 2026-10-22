# ADR-0013: Nemotron3 Diarization으로 화자 구간을 나눈 후 Qwen3-ASR으로 전사한다

## Status

Accepted

---

## Context

Meety는 회의 중 텍스트·시작/종료 시각·화자 라벨을 함께 제공하려 한다. [ADR-0012](0012-modal-realtime-stt-serving.md)에 따라 전사를 직접 서빙하며, 한국어 회의 전사와 화자 정보를 어떤 순서로 결합할지 결정해야 한다.

(실험 결과 차후 추가)

---
