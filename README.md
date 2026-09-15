# KTB4-6th-AI

Meety의 실시간 회의 전사, 질의응답, 회의 요약 및 커뮤니케이션 분석을 담당하는 AI 저장소입니다.

## 개발 환경

Python 3.12.13과 [uv](https://docs.astral.sh/uv/)를 사용합니다. 기존 화자 분리 실험의 Python 3.12 계열을 기준으로 초기 환경을 통일했습니다.

```bash
uv sync --locked
uv run python --version
```

`uv sync`는 프로젝트의 `.venv`를 생성합니다. 편집기에서도 `.venv/bin/python`을 인터프리터로 선택합니다.

의존성 추가:

```bash
uv add <package>
uv add --dev <package>
```

`pyproject.toml`, `.python-version`, `uv.lock`은 버전 관리에 포함하고 `.venv`는 제외합니다.

## 서버 실행

저장소 루트에서 실행합니다. 환경변수 없이도 로컬 기본값으로 시작합니다.
필요하면 `.env.example`을 참고해 `.env`를 만듭니다. OS 환경변수가 `.env`보다 우선합니다.
설정은 `pydantic-settings`로 검증하며 잘못된 환경명이나 로그 레벨은 시작 시 거부합니다.

```bash
# 실시간 AI
uv run uvicorn meety_ai.live:create_live_app --factory --reload --port 8000 --no-access-log

# 분석 AI (별도 터미널/프로세스)
uv run uvicorn meety_ai.analysis:create_analysis_app --factory --reload --port 8001 --no-access-log
```

## 개발 검증

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 프로젝트 문서

- [도메인 용어집](CONTEXT.md)
- [아키텍처 결정 기록](docs/adr/README.md)
- [에이전트 작업 지침](AGENTS.md)

