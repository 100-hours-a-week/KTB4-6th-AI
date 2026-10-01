"""허용한 지표와 오류 정보만 Sentry로 전송한다."""

import asyncio
import os

import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.types import Event, Hint
from structlog.contextvars import get_contextvars

from meety_ai.settings import Settings


def filter_event(event: Event, hint: Hint) -> Event:
    # 예외 메시지·체인·소스 문맥에 입력이나 URL이 섞일 수 있어 허용 필드만 남긴다.
    result = {
        key: event[key]
        for key in ("event_id", "timestamp", "platform", "level", "environment")
        if key in event
    }
    exceptions = []
    for error in event.get("exception", {}).get("values", []):
        frames = [
            {
                key: frame[key]
                for key in ("filename", "function", "lineno", "in_app")
                if key in frame
            }
            for frame in error.get("stacktrace", {}).get("frames", [])
        ]
        exceptions.append(
            {
                "type": error.get("type", "Error"),
                "stacktrace": {"frames": frames},
            }
        )
    if exceptions:
        result["exception"] = {"values": exceptions}
    context = get_contextvars()
    result["tags"] = {
        key: context[key] for key in ("service", "request_id", "connection_id") if key in context
    }
    if "error_code" in event.get("tags", {}):
        result["tags"]["error_code"] = event["tags"]["error_code"]
    return result


def create_sentry_client(settings: Settings) -> sentry_sdk.Client:
    return sentry_sdk.Client(
        dsn=settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else "",
        environment=settings.environment,
        default_integrations=False,
        auto_enabling_integrations=False,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        max_breadcrumbs=0,
        enable_logs=False,
        enable_metrics=bool(settings.sentry_dsn),
        before_send=filter_event,
    )


async def report_connections(app: FastAPI, client: sentry_sdk.Client) -> None:
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        attributes = {"service": "live", "instance": f"{os.uname().nodename}:{os.getpid()}"}
        try:
            while True:
                sentry_sdk.metrics.gauge(
                    "meety.websocket.active",
                    app.state.recording_connections,
                    attributes=attributes,
                )
                await asyncio.sleep(10)
        finally:
            sentry_sdk.metrics.gauge(
                "meety.websocket.active",
                app.state.recording_connections,
                attributes=attributes,
            )
