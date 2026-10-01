"""실시간·분석 서비스의 초기화 코드를 공유하되 실행 프로세스는 분리한다."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Literal

import structlog
from fastapi import FastAPI

from meety_ai.log_context import LogContextMiddleware
from meety_ai.logging import configure_logging
from meety_ai.observability import create_sentry_client, report_connections
from meety_ai.settings import Settings

logger = structlog.stdlib.get_logger(__name__)


def create_app(service: Literal["live", "analysis"], settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        configure_logging(settings.log_level, production=settings.environment == "production")
        client = create_sentry_client(settings)
        app.state.sentry_client = client
        reporter = (
            asyncio.create_task(report_connections(app, client))
            if service == "live" and settings.sentry_dsn
            else None
        )
        logger.info("service_started", service=service, environment=settings.environment)
        try:
            yield
        finally:
            if reporter is not None:
                reporter.cancel()
                with suppress(asyncio.CancelledError):
                    await reporter
            await asyncio.to_thread(client.close, timeout=2)
            logger.info("service_stopped", service=service)

    app = FastAPI(
        title=f"Meety AI — {service}",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(LogContextMiddleware, service=service)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        # 프로세스 응답 여부만 확인하며 외부 공급자의 준비 상태는 보장하지 않는다.
        return {"status": "ok", "service": service}

    return app
