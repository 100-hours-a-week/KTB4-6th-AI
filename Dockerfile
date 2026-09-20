# syntax=docker/dockerfile:1

# ---------- Build Stage ----------
FROM python:3.12-slim AS builder
WORKDIR /app

# uv를 설치하지 않고 공식 이미지에서 바이너리만 복사해 사용
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 의존성 레이어 캐싱: lock 파일 기준으로 프로젝트 코드 없이 의존성만 먼저 설치
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 소스 코드 복사 후 프로젝트 자체를 설치 (레이어 캐시 무효화 범위 최소화)
COPY . .
RUN uv sync --frozen --no-dev

# ---------- Runtime Stage ----------
FROM python:3.12-slim AS runtime
WORKDIR /app

# 오디오 세그먼트 포맷 변환 등에 필요한 ffmpeg 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser

# 빌드 스테이지의 가상환경(.venv)과 소스 코드를 함께 복사
COPY --from=builder --chown=appuser:appuser /app /app
ENV PATH="/app/.venv/bin:$PATH"

USER appuser
EXPOSE 8000

CMD ["uvicorn", "meety_ai.live:create_live_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
