"""Testes unitários de validação do synetra.config.

Os testes existentes em `test_transformer.py` validam o config.toml real.
Este arquivo cobre casos isolados: bordas de validação, constantes e
mensagens de erro.

Princípios F.I.R.S.T.: valida cada modelo Pydantic sem I/O.
"""
from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from synetra.config import (
    _DEFAULT_BATCH_SIZE,
    _DEFAULT_CACHE_DAYS,
    _DEFAULT_WORKERS,
    _MAX_FISCAL_YEAR,
    _MIN_FISCAL_YEAR,
    _REQUIRED_SECTORS,
    _YEAR_PLACEHOLDER,
    MarketConfig,
    PipelineConfig,
    SetoresConfig,
    SynetraConfig,
    UrlsConfig,
    load_config,
)
from synetra.domain import Categoria

# --- Constantes ---


class TestConfigConstants:
    """Valores semânticos das constantes de validação."""

    def test_required_sectors_match_categoria_enum(self) -> None:
        """Setores obrigatórios derivam do enum Categoria."""
        expected = {cat.value.lower() for cat in Categoria}
        assert frozenset(expected) == _REQUIRED_SECTORS

    def test_year_placeholder(self) -> None:
        assert _YEAR_PLACEHOLDER == "{year}"

    def test_fiscal_year_range_is_sensible(self) -> None:
        assert _MIN_FISCAL_YEAR < _MAX_FISCAL_YEAR
        assert _MIN_FISCAL_YEAR >= 2000  # CVM começou digitalização em ~2000

    def test_defaults_are_set(self) -> None:
        assert _DEFAULT_WORKERS > 0
        assert _DEFAULT_CACHE_DAYS > 0
        assert _DEFAULT_BATCH_SIZE > 0


# --- UrlsConfig ---


class TestUrlsConfig:
    """Validação de URLs com placeholder {year}."""

    def test_valid_urls_pass(self) -> None:
        urls = UrlsConfig(
            dfp_pattern="https://example.com/dfp/{year}.zip",
            fre_pattern="https://example.com/fre/{year}.zip",
            cadastro="https://example.com/cad.csv",
        )
        assert urls.dfp_pattern.startswith("https://")

    def test_missing_year_placeholder_fails(self) -> None:
        with pytest.raises(ValidationError) as exc:
            UrlsConfig(
                dfp_pattern="https://example.com/dfp.zip",  # sem {year}
                fre_pattern="https://example.com/fre/{year}.zip",
                cadastro="https://example.com/cad.csv",
            )
        assert "{year}" in str(exc.value)

    def test_cadastro_does_not_need_placeholder(self) -> None:
        """Cadastro é arquivo único — não precisa de {year}."""
        urls = UrlsConfig(
            dfp_pattern="https://x.com/{year}.zip",
            fre_pattern="https://x.com/fre/{year}.zip",
            cadastro="https://x.com/cad.csv",  # sem {year}, OK
        )
        assert urls.cadastro.endswith("cad.csv")

    def test_frozen_model(self) -> None:
        """Config é imutável — evita mutação acidental no pipeline."""
        urls = UrlsConfig(
            dfp_pattern="https://x.com/{year}.zip",
            fre_pattern="https://x.com/fre/{year}.zip",
            cadastro="https://x.com/cad.csv",
        )
        with pytest.raises(ValidationError):
            urls.dfp_pattern = "https://novo.com/{year}.zip"  # type: ignore[misc]


# --- PipelineConfig ---


class TestPipelineConfig:
    """Validação de anos, workers e tipos de documento."""

    def test_valid_pipeline(self) -> None:
        cfg = PipelineConfig(
            doc_types=["DRE", "BPA"],
            years_start=2010,
            years_end=2024,
        )
        assert cfg.max_workers == _DEFAULT_WORKERS
        assert cfg.force_refresh is False

    def test_end_must_be_after_start(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PipelineConfig(
                doc_types=["DRE"],
                years_start=2020,
                years_end=2020,  # igual, não aceita
            )
        assert "years_end" in str(exc.value)

    def test_end_less_than_start_fails(self) -> None:
        with pytest.raises(ValidationError):
            PipelineConfig(
                doc_types=["DRE"],
                years_start=2024,
                years_end=2020,
            )

    def test_empty_doc_types_fails(self) -> None:
        with pytest.raises(ValidationError):
            PipelineConfig(
                doc_types=[],
                years_start=2010,
                years_end=2024,
            )

    def test_year_below_range_fails(self) -> None:
        with pytest.raises(ValidationError):
            PipelineConfig(
                doc_types=["DRE"],
                years_start=1999,  # abaixo do mínimo
                years_end=2024,
            )

    def test_max_workers_out_of_range_fails(self) -> None:
        with pytest.raises(ValidationError):
            PipelineConfig(
                doc_types=["DRE"],
                years_start=2010,
                years_end=2024,
                max_workers=100,  # acima do limite
            )


# --- SetoresConfig ---


class TestSetoresConfig:
    """Palavras-chave para classificação setorial."""

    def test_valid_setores(self) -> None:
        s = SetoresConfig(
            financeiro=["BANCO", "FINANCEIRA"],
            seguradora=["SEGURO"],
        )
        assert len(s.financeiro) == 2

    def test_empty_financeiro_fails(self) -> None:
        with pytest.raises(ValidationError):
            SetoresConfig(financeiro=[], seguradora=["SEGURO"])


# --- MarketConfig (defaults) ---


class TestMarketConfig:
    def test_defaults(self) -> None:
        m = MarketConfig()
        assert m.enabled is False
        assert m.cache_max_age_days == _DEFAULT_CACHE_DAYS
        assert m.batch_size == _DEFAULT_BATCH_SIZE


# --- SynetraConfig (validação completa) ---


def _minimal_raw_config() -> dict:
    """Config válido mínimo para testes da raiz."""
    return {
        "urls": {
            "dfp_pattern": "https://x.com/dfp/{year}.zip",
            "fre_pattern": "https://x.com/fre/{year}.zip",
            "cadastro": "https://x.com/cad.csv",
        },
        "pipeline": {
            "doc_types": ["DRE"],
            "years_start": 2010,
            "years_end": 2024,
        },
        "contas": {
            "industrial": {"1.01": "ATIVO_TOTAL"},
            "financeiro": {"1.01": "ATIVO_TOTAL"},
            "seguradora": {"1.01": "ATIVO_TOTAL"},
        },
        "setores": {
            "financeiro": ["BANCO"],
            "seguradora": ["SEGURO"],
        },
        "regex": {
            "depreciacao": "DEPREC",
            "capex": "CAPEX",
            "capex_tipo": "TIPO",
            "dividendos": "DIV",
            "ebit_seguradora": "EBIT",
        },
    }


class TestSynetraConfigRoot:
    """Validação da configuração raiz."""

    def test_minimal_config_passes(self) -> None:
        config = SynetraConfig(**_minimal_raw_config())
        assert config.urls.dfp_pattern.startswith("https://")
        assert config.market.enabled is False  # default

    def test_missing_industrial_sector_fails(self) -> None:
        raw = _minimal_raw_config()
        del raw["contas"]["industrial"]
        with pytest.raises(ValidationError) as exc:
            SynetraConfig(**raw)
        assert "industrial" in str(exc.value).lower()

    def test_missing_financeiro_sector_fails(self) -> None:
        raw = _minimal_raw_config()
        del raw["contas"]["financeiro"]
        with pytest.raises(ValidationError):
            SynetraConfig(**raw)

    def test_missing_seguradora_sector_fails(self) -> None:
        raw = _minimal_raw_config()
        del raw["contas"]["seguradora"]
        with pytest.raises(ValidationError):
            SynetraConfig(**raw)


# --- load_config ---


class TestLoadConfig:
    """Carregamento do arquivo TOML."""

    def test_file_not_found_raises(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nao_existe.toml"))

    def test_loads_valid_toml(self, tmp_path: pathlib.Path) -> None:
        toml_content = """
[urls]
dfp_pattern = "https://x.com/dfp/{year}.zip"
fre_pattern = "https://x.com/fre/{year}.zip"
cadastro = "https://x.com/cad.csv"

[pipeline]
doc_types = ["DRE"]
years_start = 2010
years_end = 2024

[contas.industrial]
"1.01" = "ATIVO_TOTAL"

[contas.financeiro]
"1.01" = "ATIVO_TOTAL"

[contas.seguradora]
"1.01" = "ATIVO_TOTAL"

[setores]
financeiro = ["BANCO"]
seguradora = ["SEGURO"]

[regex]
depreciacao = "DEPREC"
capex = "CAPEX"
capex_tipo = "TIPO"
dividendos = "DIV"
ebit_seguradora = "EBIT"
"""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(toml_content, encoding="utf-8")

        config = load_config(str(toml_file))
        assert config.pipeline.years_start == 2010
        assert config.pipeline.years_end == 2024

    def test_invalid_toml_syntax_raises(self, tmp_path: pathlib.Path) -> None:
        """TOML malformado deve propagar TOMLDecodeError."""
        import tomllib

        toml_file = tmp_path / "config.toml"
        toml_file.write_text("this is = not valid toml === syntax", encoding="utf-8")

        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(str(toml_file))
