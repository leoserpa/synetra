"""Testes unitários para os helpers de synetra.market.yahoo_client.

Cobre:
    - _ParquetCache (dataclass genérico de cache)
    - _optional_float (conversão segura com NaN)
    - _parse_yahoo_ohlcv_row (normaliza linha pandas → dict)
    - _to_yahoo_tickers (sufixo .SA)

Princípios F.I.R.S.T.: sem I/O de rede, fixtures minúsculas, mocks explícitos.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import polars as pl
import pytest

from synetra.market.yahoo_client import (
    _DEFAULT_START_DATE,
    _SECONDS_PER_DAY,
    _YAHOO_SA_SUFFIX,
    _optional_float,
    _ParquetCache,
    _parse_yahoo_ohlcv_row,
    _to_yahoo_tickers,
)

# --- Constantes do módulo ---


class TestModuleConstants:
    """Garante que as constantes têm valores semanticamente corretos."""

    def test_seconds_per_day(self) -> None:
        assert _SECONDS_PER_DAY == 86_400

    def test_yahoo_sa_suffix(self) -> None:
        assert _YAHOO_SA_SUFFIX == ".SA"

    def test_default_start_date_is_parseable(self) -> None:
        """Data default precisa ser ISO 8601 válida."""
        parsed = datetime.strptime(_DEFAULT_START_DATE, "%Y-%m-%d")
        assert parsed.year >= 2000


# --- _to_yahoo_tickers ---


class TestToYahooTickers:
    """Adiciona sufixo .SA a tickers B3."""

    def test_adds_suffix_to_each_ticker(self) -> None:
        assert _to_yahoo_tickers(["PETR4", "VALE3"]) == ["PETR4.SA", "VALE3.SA"]

    def test_empty_list_returns_empty(self) -> None:
        assert _to_yahoo_tickers([]) == []


# --- _optional_float ---


class TestOptionalFloat:
    """Converte valores para float ou retorna None para NaN/inválidos."""

    def test_int_returns_float(self) -> None:
        assert _optional_float(42) == pytest.approx(42.0)

    def test_float_returns_itself(self) -> None:
        assert _optional_float(3.14) == pytest.approx(3.14)

    def test_nan_returns_none(self) -> None:
        assert _optional_float(float("nan")) is None

    def test_none_returns_none(self) -> None:
        assert _optional_float(None) is None

    def test_string_number_is_coerced(self) -> None:
        assert _optional_float("10.5") == pytest.approx(10.5)

    def test_invalid_string_returns_none(self) -> None:
        assert _optional_float("banana") is None


# --- _parse_yahoo_ohlcv_row ---


class TestParseYahooOhlcvRow:
    """Normaliza uma linha de OHLCV do pandas para dict padronizado."""

    @pytest.fixture
    def sample_row(self) -> pd.Series:
        return pd.Series(
            {
                "Date": pd.Timestamp("2024-05-10"),
                "Open": 10.0,
                "High": 10.5,
                "Low": 9.8,
                "Close": 10.3,
                "Adj Close": 10.2,
                "Volume": 1_000_000,
            }
        )

    def test_full_row_produces_full_dict(self, sample_row: pd.Series) -> None:
        result = _parse_yahoo_ohlcv_row("PETR4", sample_row)
        assert result == {
            "TICKER": "PETR4",
            "DATA": "2024-05-10",
            "ABERTURA": 10.0,
            "MAXIMA": 10.5,
            "MINIMA": 9.8,
            "FECHAMENTO": 10.3,
            "FECHAMENTO_AJUSTADO": 10.2,
            "VOLUME": 1_000_000.0,
        }

    def test_nan_close_returns_none(self, sample_row: pd.Series) -> None:
        """Sem Close não há cotação válida — deve retornar None."""
        sample_row["Close"] = float("nan")
        assert _parse_yahoo_ohlcv_row("PETR4", sample_row) is None

    def test_missing_adj_close_falls_back_to_close(
        self, sample_row: pd.Series
    ) -> None:
        """Quando Adj Close é NaN, usa Close como fallback."""
        sample_row["Adj Close"] = float("nan")
        result = _parse_yahoo_ohlcv_row("PETR4", sample_row)
        assert result is not None
        assert result["FECHAMENTO_AJUSTADO"] == pytest.approx(10.3)

    def test_missing_volume_defaults_to_zero(self, sample_row: pd.Series) -> None:
        sample_row["Volume"] = float("nan")
        result = _parse_yahoo_ohlcv_row("PETR4", sample_row)
        assert result is not None
        assert result["VOLUME"] == pytest.approx(0.0)

    def test_optional_ohlc_becomes_none(self, sample_row: pd.Series) -> None:
        """Open/High/Low podem vir NaN em alguns pregões — vira None."""
        sample_row["Open"] = float("nan")
        sample_row["High"] = float("nan")
        result = _parse_yahoo_ohlcv_row("PETR4", sample_row)
        assert result is not None
        assert result["ABERTURA"] is None
        assert result["MAXIMA"] is None

    def test_date_is_formatted_iso(self, sample_row: pd.Series) -> None:
        result = _parse_yahoo_ohlcv_row("PETR4", sample_row)
        assert result is not None
        assert result["DATA"] == "2024-05-10"


# --- _ParquetCache ---


class TestParquetCache:
    """Gerenciador genérico de cache Parquet com expiração por idade."""

    def test_is_not_fresh_when_file_missing(self, tmp_path) -> None:
        cache = _ParquetCache(
            path=tmp_path / "missing.parquet",
            max_age_seconds=_SECONDS_PER_DAY,
            label="test",
        )
        assert cache.is_fresh() is False
        assert cache.load() is None

    def test_is_fresh_after_save(self, tmp_path) -> None:
        cache = _ParquetCache(
            path=tmp_path / "c.parquet",
            max_age_seconds=_SECONDS_PER_DAY,
            label="test",
        )
        df = pl.DataFrame({"x": [1, 2, 3]})
        cache.save(df)

        assert cache.is_fresh() is True
        loaded = cache.load()
        assert loaded is not None
        assert loaded.height == 3

    def test_zero_max_age_means_never_fresh(self, tmp_path) -> None:
        """max_age_seconds=0 força rebaixamento sempre."""
        cache = _ParquetCache(
            path=tmp_path / "c.parquet",
            max_age_seconds=0,
            label="test",
        )
        df = pl.DataFrame({"x": [1]})
        cache.save(df)
        assert cache.is_fresh() is False
        assert cache.load() is None

    def test_expired_cache_returns_none(self, tmp_path) -> None:
        """Arquivo velho além de max_age_seconds deve retornar None."""
        import os

        # 1 hora de validade
        one_hour = 3600
        cache = _ParquetCache(
            path=tmp_path / "c.parquet",
            max_age_seconds=one_hour,
            label="test",
        )
        cache.save(pl.DataFrame({"x": [1]}))

        # Envelhece o arquivo em 2 horas
        old_mtime = time.time() - 2 * one_hour
        os.utime(cache.path, (old_mtime, old_mtime))

        assert cache.is_fresh() is False
        assert cache.load() is None

    def test_corrupted_file_returns_none_without_raise(self, tmp_path) -> None:
        """Parquet corrompido não deve propagar exceção — apenas retorna None."""
        cache_path = tmp_path / "c.parquet"
        cache_path.write_bytes(b"este nao eh um parquet valido")
        cache = _ParquetCache(
            path=cache_path,
            max_age_seconds=_SECONDS_PER_DAY,
            label="test",
        )
        assert cache.is_fresh() is True  # existe e é recente
        assert cache.load() is None  # mas falha silenciosamente

    def test_is_frozen_dataclass(self) -> None:
        """Dataclass imutável evita mutação acidental."""
        import pathlib

        cache = _ParquetCache(
            path=pathlib.Path("/tmp/x.parquet"),
            max_age_seconds=100,
            label="x",
        )
        with pytest.raises(AttributeError):
            cache.max_age_seconds = 200  # type: ignore[misc]
