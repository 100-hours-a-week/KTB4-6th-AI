"""프로세스 설정. 공급자 API 키는 `.env`의 기존 이름(접두사 없음)을 그대로 읽는다."""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEETY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # 미정의 .env 항목이 거부될 때 오류 메시지에 값 원문이 찍히지 않게 한다.
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # 공급자 키는 MEETY_ 접두사를 쓰지 않으므로 환경변수 이름을 명시한다.
    # 값은 SecretStr이라 repr/str과 로그에 원문이 남지 않으며, 키가 없어도 앱은 뜬다.
    # 단, ValidationError.errors()/json()은 include_input=False로 호출해야 원문이 빠진다.
    speechmatics_api_key: SecretStr | None = Field(
        default=None, validation_alias="SPEECHMATICS_API_KEY"
    )