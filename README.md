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

`pyproject.toml`, `.python-version`, `uv.lock`은 버전 관리에 포함하고 `.venv`는 제외합니다. 현재는 Python 환경만 준비되어 있으며 서비스 코드와 실행 의존성은 구현 시 추가합니다.

## 프로젝트 문서

- [도메인 용어집](CONTEXT.md)
- [아키텍처 결정 기록](docs/adr/README.md)
- [에이전트 작업 지침](AGENTS.md)
