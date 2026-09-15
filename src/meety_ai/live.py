"""Live service entry point."""

from fastapi import FastAPI

from meety_ai.app import create_app


def create_live_app() -> FastAPI:
    return create_app("live")
