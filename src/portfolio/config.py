"""Чтение .env, пути и константы. Логики здесь нет (спека 9)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Настройки приложения. Значения по умолчанию годятся для локального запуска."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://portfolio:portfolio@localhost:5432/portfolio",
    )

    data_raw_dir: Path = Field(default=Path("data/raw"))
    data_input_dir: Path = Field(default=Path("data/input"))

    tax_rate: Decimal = Field(default=Decimal("0.13"))

    # Пороги сверки (спека 5.2). Бумаги сверяются точно, деньги — до копейки.
    reconcile_cash_tolerance: Decimal = Field(default=Decimal("0.01"))
    reconcile_quantity_tolerance: Decimal = Field(default=Decimal("0"))

    @property
    def raw_dir(self) -> Path:
        return self._absolute(self.data_raw_dir)

    @property
    def input_dir(self) -> Path:
        return self._absolute(self.data_input_dir)

    @staticmethod
    def _absolute(path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
