"""Analysis service entry point; feature routes will be added later."""

from fastapi import FastAPI

from meety_ai.app import create_app


def create_analysis_app() -> FastAPI:
    return create_app("analysis")
