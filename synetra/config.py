"""Carregador e validador de configuração TOML com Pydantic."""
import pathlib
from typing import Dict, List

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from pydantic import BaseModel, Field, field_validator


# ============================================================
# Modelos Pydantic (Validação Tipada)
# ============================================================

class UrlsConfig(BaseModel):
    """URLs dos dados da CVM e Fundamentus."""
    dfp_pattern: str
    fre_pattern: str
    cadastro: str
    fundamentus: str

    @field_validator('dfp_pattern', 'fre_pattern')
    @classmethod
    def must_contain_year_placeholder(cls, v: str) -> str:
        if '{year}' not in v:
            raise ValueError(f"URL pattern deve conter '{{year}}' como placeholder: {v}")
        return v


class PipelineConfig(BaseModel):
    """Parametros do pipeline de processamento."""
    doc_types: List[str] = Field(min_length=1)
    years_start: int = Field(ge=2000, le=2099)
    years_end: int = Field(ge=2000, le=2099)
    max_workers: int = Field(ge=1, le=20, default=5)

    @field_validator('years_end')
    @classmethod
    def end_must_be_after_start(cls, v: int, info) -> int:
        if 'years_start' in info.data and v <= info.data['years_start']:
            raise ValueError(f"years_end ({v}) deve ser maior que years_start ({info.data['years_start']})")
        return v


class FuzzyMatchConfig(BaseModel):
    """Configuração do fuzzy matching."""
    threshold: int = Field(ge=0, le=100, default=85)


class SetoresConfig(BaseModel):
    """Palavras-chave para classificação setorial."""
    financeiro: List[str] = Field(min_length=1)
    seguradora: List[str] = Field(min_length=1)


class SynetraConfig(BaseModel):
    """Configuração raiz do Synetra. Valida todo o parameters.toml."""
    urls: UrlsConfig
    pipeline: PipelineConfig
    fuzzy_match: FuzzyMatchConfig
    contas: Dict[str, Dict[str, str]]
    setores: SetoresConfig

    @field_validator('contas')
    @classmethod
    def must_have_required_sectors(cls, v: dict) -> dict:
        required = {'industrial', 'financeiro', 'seguradora'}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(f"Setores obrigatórios faltando em [contas]: {missing}")
        return v


# ============================================================
# Funções Públicas
# ============================================================

def load_config(config_path: str = "parameters.toml") -> dict:
    """
    Carrega o arquivo TOML, valida com Pydantic e retorna como dicionário.
    Levanta erros claros se a configuração estiver incompleta ou inválida.
    """
    path = pathlib.Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {config_path}")
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    # Validação Pydantic (levanta ValidationError com detalhes)
    validated = SynetraConfig(**raw)
    return validated.model_dump()
