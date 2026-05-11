"""Carregador e validador de configuração TOML com Pydantic.

Exposições principais:

    - :class:`SynetraConfig` — modelo raiz que valida o ``config.toml`` inteiro.
    - :func:`load_config` — função de entrada do pipeline.

Todos os modelos são imutáveis (``frozen=True``): configuração carregada
uma vez no início do pipeline e nunca mutada ao longo da execução.
"""
from __future__ import annotations

import pathlib
import tomllib
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from synetra.domain import Categoria

# --- Constantes de validação ---

#: Setores obrigatórios na seção ``[contas]`` do ``config.toml``.
#: Deriva-se diretamente do enum ``Categoria`` para manter sincronia.
_REQUIRED_SECTORS: Final[frozenset[str]] = frozenset(
    cat.value.lower() for cat in Categoria
)

#: Placeholder obrigatório nas URLs padrão (substituído pelo ano no runtime).
_YEAR_PLACEHOLDER: Final[str] = "{year}"

# Intervalos de anos aceitos para o pipeline (evita datas absurdas).
_MIN_FISCAL_YEAR: Final[int] = 2000
_MAX_FISCAL_YEAR: Final[int] = 2099

# Limites de concorrência para evitar abuso do servidor CVM.
_MIN_WORKERS: Final[int] = 1
_MAX_WORKERS: Final[int] = 20
_DEFAULT_WORKERS: Final[int] = 5

# Cache do mercado (Yahoo Finance).
_DEFAULT_CACHE_DAYS: Final[int] = 7
_MIN_CACHE_DAYS: Final[int] = 1
_MAX_CACHE_DAYS: Final[int] = 365

_DEFAULT_BATCH_SIZE: Final[int] = 100
_MIN_BATCH_SIZE: Final[int] = 10
_MAX_BATCH_SIZE: Final[int] = 500


# --- Modelos Pydantic ---


class UrlsConfig(BaseModel):
    """URLs dos dados da CVM (devem conter ``{year}``)."""

    model_config = ConfigDict(frozen=True)
    dfp_pattern: str
    fre_pattern: str
    cadastro: str

    @field_validator("dfp_pattern", "fre_pattern")
    @classmethod
    def must_contain_year_placeholder(cls, v: str) -> str:
        if _YEAR_PLACEHOLDER not in v:
            raise ValueError(
                f"URL pattern deve conter '{_YEAR_PLACEHOLDER}' como placeholder: {v}"
            )
        return v


class PipelineConfig(BaseModel):
    """Parâmetros do pipeline de processamento."""

    model_config = ConfigDict(frozen=True)
    doc_types: list[str] = Field(min_length=1)
    years_start: int = Field(ge=_MIN_FISCAL_YEAR, le=_MAX_FISCAL_YEAR)
    years_end: int = Field(ge=_MIN_FISCAL_YEAR, le=_MAX_FISCAL_YEAR)
    max_workers: int = Field(
        ge=_MIN_WORKERS, le=_MAX_WORKERS, default=_DEFAULT_WORKERS
    )
    force_refresh: bool = False

    @field_validator("years_end")
    @classmethod
    def end_must_be_after_start(cls, v: int, info: Any) -> int:
        start = info.data.get("years_start")
        if start is not None and v <= start:
            raise ValueError(
                f"years_end ({v}) deve ser maior que years_start ({start})"
            )
        return v


class SetoresConfig(BaseModel):
    """Palavras-chave para classificação setorial."""

    model_config = ConfigDict(frozen=True)
    financeiro: list[str] = Field(min_length=1)
    seguradora: list[str] = Field(min_length=1)


class RegexConfig(BaseModel):
    """Padrões Regex para detecção de contas especiais."""

    model_config = ConfigDict(frozen=True)
    depreciacao: str
    capex: str
    capex_tipo: str
    dividendos: str
    ebit_seguradora: str


class MarketConfig(BaseModel):
    """Configuração da integração com Yahoo Finance (cotações de mercado).

    O snapshot atual é derivado do último fechamento do histórico diário,
    sem chamada adicional à API. Não há mais configuração de "intraday".
    """

    model_config = ConfigDict(frozen=True)
    enabled: bool = False
    cache_max_age_days: int = Field(
        ge=_MIN_CACHE_DAYS, le=_MAX_CACHE_DAYS, default=_DEFAULT_CACHE_DAYS
    )
    batch_size: int = Field(
        ge=_MIN_BATCH_SIZE, le=_MAX_BATCH_SIZE, default=_DEFAULT_BATCH_SIZE
    )


class SynetraConfig(BaseModel):
    """Configuração raiz do Synetra. Valida o ``config.toml`` inteiro."""

    model_config = ConfigDict(frozen=True)
    urls: UrlsConfig
    pipeline: PipelineConfig
    contas: dict[str, dict[str, str]]
    setores: SetoresConfig
    regex: RegexConfig
    market: MarketConfig = Field(default_factory=MarketConfig)

    @field_validator("contas")
    @classmethod
    def must_have_required_sectors(cls, v: dict) -> dict:
        missing = _REQUIRED_SECTORS - set(v.keys())
        if missing:
            raise ValueError(
                f"Setores obrigatórios faltando em [contas]: {sorted(missing)}"
            )
        return v


# --- API pública ---


def load_config(config_path: str = "config.toml") -> SynetraConfig:
    """Carrega o arquivo TOML, valida com Pydantic e retorna ``SynetraConfig``.

    Args:
        config_path: Caminho do arquivo TOML.

    Returns:
        Instância imutável de ``SynetraConfig`` pronta para uso.

    Raises:
        FileNotFoundError: Arquivo inexistente.
        tomllib.TOMLDecodeError: TOML malformado (syntax error).
        pydantic.ValidationError: TOML parseia, mas campos inválidos.
    """
    path = pathlib.Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {config_path}"
        )

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    return SynetraConfig(**raw)
