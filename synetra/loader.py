"""Leitura de arquivos CSV/Parquet da CVM e validação de schema.

Responsabilidades:

    - Extrair CSVs dos ZIPs distribuídos pela CVM (DFP e FRE).
    - Consolidar documentos de um ano aplicando regras de prioridade
      (Consolidado > Individual, DFC_MI > DFC_MD).
    - Converter escala monetária (MIL → unidades).
    - Carregar Parquets já processados com predicate pushdown.
    - Baixar e cachear cadastro de setores econômicos.

API pública:
    - :func:`read_cvm_csv`
    - :func:`process_year_from_zip`
    - :func:`process_fre_from_zip`
    - :func:`load_parquet_history`
    - :func:`get_sectors`
"""
from __future__ import annotations

import pathlib
import urllib.error
import zipfile
from collections.abc import Sequence
from typing import Final

import polars as pl
from loguru import logger

# --- Constantes ---

#: Colunas mínimas que um CSV da CVM precisa ter para ser considerado válido.
REQUIRED_COLUMNS: Final[list[str]] = [
    "CD_CONTA", "CNPJ_CIA", "VL_CONTA", "ORDEM_EXERC", "DS_CONTA",
]

#: Filtro de ordem de exercício: mantemos apenas a última versão disponível.
_ORDEM_ULTIMO: Final[str] = "ÚLTIMO"

#: Fator de conversão aplicado quando ``ESCALA_MOEDA`` contém "MIL".
_SCALE_FACTOR_MIL: Final[float] = 1000.0

#: Substring usado para detectar escala em milhares (case-insensitive).
_SCALE_KEYWORD_MIL: Final[str] = "MIL"

# Prioridades de deduplicação — quanto MENOR, maior a preferência na ordenação ascendente.
_PRIORITY_CONSOLIDATED: Final[int] = 1
"""Balanço consolidado do grupo (preferido)."""

_PRIORITY_INDIVIDUAL: Final[int] = 2
"""Balanço individual da controladora (fallback)."""

# Prioridade dentro dos tipos de DFC (Demonstração de Fluxo de Caixa):
_PRIORITY_DFC_INDIRECT: Final[int] = 1
"""DFC Método Indireto (DFC_MI) — preferido, mais usado no Brasil."""

_PRIORITY_DFC_DIRECT: Final[int] = 2
"""DFC Método Direto (DFC_MD) — usado apenas quando DFC_MI não está disponível."""

_PRIORITY_NON_DFC: Final[int] = 0
"""Documentos não-DFC (DRE, BPA, BPP) — não competem por prioridade."""

#: Mapa de tipo de documento → prioridade DFC.
_DFC_PRIORITY_MAP: Final[dict[str, int]] = {
    "DFC_MI": _PRIORITY_DFC_INDIRECT,
    "DFC_MD": _PRIORITY_DFC_DIRECT,
}

#: Tipos de capital mantidos no FRE (preferimos "Integralizado", mas aceitamos "Emitido").
_FRE_CAPITAL_TYPES: Final[list[str]] = ["Capital Integralizado", "Capital Emitido"]

#: Coluna agregadora que normaliza os tipos de DFC sob um único rótulo ``DFC``.
_DOC_TYPE_DFC: Final[str] = "DFC"


# --- Helpers internos ---


def _decode_cvm_bytes(raw_bytes: bytes) -> bytes:
    """Decodifica bytes latin1 → UTF-8 substituindo caracteres corrompidos.

    A CVM publica arquivos em latin1 mas ocasionalmente com bytes inválidos.
    ``errors='replace'`` evita crashes em caracteres corrompidos.
    """
    return raw_bytes.decode("latin1", errors="replace").encode("utf8")


def _normalize_doc_type(doc_type: str) -> str:
    """Agrupa DFC_MI e DFC_MD sob o rótulo ``DFC`` (preserva demais como estão)."""
    return _DOC_TYPE_DFC if _DOC_TYPE_DFC in doc_type else doc_type


def _dfc_priority(doc_type: str) -> int:
    """Retorna prioridade DFC para um tipo de documento.

    Documentos não-DFC retornam ``_PRIORITY_NON_DFC`` (zero) e por isso
    não interferem na ordenação final.
    """
    return _DFC_PRIORITY_MAP.get(doc_type, _PRIORITY_NON_DFC)


def _apply_monetary_scale(df: pl.DataFrame) -> pl.DataFrame:
    """Multiplica ``VL_CONTA`` por 1000 quando ``ESCALA_MOEDA`` contém "MIL"."""
    value_as_float = pl.col("VL_CONTA").cast(pl.Float64, strict=False)
    is_mil_scale = pl.col("ESCALA_MOEDA").str.to_uppercase().str.contains(
        _SCALE_KEYWORD_MIL
    )
    return df.with_columns(
        pl.when(is_mil_scale)
        .then(value_as_float * _SCALE_FACTOR_MIL)
        .otherwise(value_as_float)
        .alias("VL_CONTA")
    )


def _warn_missing_columns(filename: str, df: pl.DataFrame) -> None:
    """Loga aviso quando o CSV da CVM não tem todas as colunas esperadas."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        logger.warning("Colunas faltando em {}: {}", filename, missing)


# --- API pública: leitura de CSVs do ZIP CVM ---


def read_cvm_csv(zip_ref: zipfile.ZipFile, filename: str) -> pl.DataFrame:
    """Lê um CSV da CVM de dentro de um ZIP e retorna um DataFrame Polars.

    Args:
        zip_ref: ZipFile aberto (ex: resultado de
            ``CVMDownloader._download_single``).
        filename: Nome do CSV dentro do ZIP (ex:
            ``"dfp_cia_aberta_DRE_con_2024.csv"``).

    Returns:
        DataFrame com as colunas originais do CSV. Retorna DataFrame vazio se
        o arquivo não existir no ZIP (comportamento esperado para alguns anos).

    Note:
        Decodifica latin1 → utf8 forçadamente para evitar crashes em caracteres
        corrompidos que aparecem ocasionalmente nos dados brutos da CVM.
    """
    try:
        with zip_ref.open(filename) as f:
            clean_bytes = _decode_cvm_bytes(f.read())
    except KeyError:
        logger.debug(
            "Arquivo não encontrado no ZIP: {} (esperado para alguns anos)", filename
        )
        return pl.DataFrame()

    df = pl.read_csv(
        clean_bytes,
        separator=";",
        infer_schema_length=0,
        null_values=["", "NA"],
        quote_char=None,
    )

    _warn_missing_columns(filename, df)

    return df.with_columns(
        pl.col("CD_CONTA").str.strip_chars(),
        pl.col("CNPJ_CIA").str.strip_chars(),
    )


# --- API pública: processamento de um ano fiscal ---


def process_year_from_zip(
    zip_ref: zipfile.ZipFile,
    year: int,
    doc_types: Sequence[str],
) -> pl.DataFrame:
    """Lê e consolida todos os documentos DFP de um ano a partir de um ZipFile.

    Lógica de prioridade:
        1. Consolidado (``con``) tem prioridade sobre Individual (``ind``).
        2. Para DFC, DFC_MI (Método Indireto) tem prioridade sobre DFC_MD (Direto).
        3. Mantém apenas a última versão (``ORDEM_EXERC == 'ÚLTIMO'``).

    Args:
        zip_ref: ZipFile da CVM (aberto).
        year: Ano do exercício.
        doc_types: Lista de tipos a processar (ex:
            ``["DRE", "BPA", "BPP", "DFC_MI", "DFC_MD"]``).

    Returns:
        DataFrame consolidado com ``VL_CONTA`` já convertido (mil → unidade) e
        deduplicado por ``(CNPJ_CIA, CD_CONTA)``. Vazio se não encontrar
        nenhum dado válido.
    """
    doc_dfs: list[pl.DataFrame] = []
    for doc_type in doc_types:
        df_doc = _load_doc_type_for_year(zip_ref, year, doc_type)
        if not df_doc.is_empty():
            doc_dfs.append(df_doc)

    if not doc_dfs:
        logger.warning("Nenhum dado valido encontrado para o ano {}", year)
        return pl.DataFrame()

    df_all = pl.concat(doc_dfs, how="diagonal_relaxed").filter(
        pl.col("ORDEM_EXERC") == _ORDEM_ULTIMO
    )

    df_deduped = _deduplicate_by_priority(df_all)
    return _apply_monetary_scale(df_deduped)


def _load_doc_type_for_year(
    zip_ref: zipfile.ZipFile, year: int, doc_type: str
) -> pl.DataFrame:
    """Carrega um tipo de documento (DRE/BPA/etc.) para um ano específico.

    Tenta consolidado e individual; concatena os dois marcando prioridade
    para deduplicação posterior.
    """
    dfs_to_concat: list[pl.DataFrame] = []

    df_con = read_cvm_csv(zip_ref, f"dfp_cia_aberta_{doc_type}_con_{year}.csv")
    if not df_con.is_empty():
        dfs_to_concat.append(
            df_con.with_columns(pl.lit(_PRIORITY_CONSOLIDATED).alias("PRIORIDADE_TIPO"))
        )

    df_ind = read_cvm_csv(zip_ref, f"dfp_cia_aberta_{doc_type}_ind_{year}.csv")
    if not df_ind.is_empty():
        dfs_to_concat.append(
            df_ind.with_columns(pl.lit(_PRIORITY_INDIVIDUAL).alias("PRIORIDADE_TIPO"))
        )

    if not dfs_to_concat:
        return pl.DataFrame()

    return pl.concat(dfs_to_concat, how="diagonal_relaxed").with_columns(
        pl.lit(_normalize_doc_type(doc_type)).alias("DOC_TYPE"),
        pl.lit(_dfc_priority(doc_type)).alias("PRIORIDADE_DFC"),
        pl.lit(year).alias("ANO"),
    )


def _deduplicate_by_priority(df: pl.DataFrame) -> pl.DataFrame:
    """Mantém apenas 1 linha por ``(CNPJ_CIA, CD_CONTA)`` usando as prioridades.

    Ordem de ordenação (crescente para prioridades, decrescente para versão):
        1. ``CNPJ_CIA``, ``CD_CONTA`` (agrupamento)
        2. ``PRIORIDADE_TIPO`` crescente → Consolidado (1) antes de Individual (2)
        3. ``PRIORIDADE_DFC`` crescente → DFC_MI (1) antes de DFC_MD (2)
        4. ``VERSAO`` decrescente (se existir) → versão mais recente primeiro
    """
    sort_cols: list[str] = ["CNPJ_CIA", "CD_CONTA", "PRIORIDADE_TIPO", "PRIORIDADE_DFC"]
    sort_desc: list[bool] = [False, False, False, False]

    if "VERSAO" in df.columns:
        sort_cols.append("VERSAO")
        sort_desc.append(True)

    aux_cols = ["PRIORIDADE_DFC", "PRIORIDADE_TIPO"]
    return (
        df.sort(sort_cols, descending=sort_desc)
        .unique(subset=["CNPJ_CIA", "CD_CONTA"], keep="first")
        .drop([c for c in aux_cols if c in df.columns])
    )


# --- API pública: processamento de FRE (capital social) ---


def process_fre_from_zip(zip_ref: zipfile.ZipFile, year: int) -> pl.DataFrame:
    """Lê dados de quantidade de ações (FRE) a partir de um ZipFile.

    Args:
        zip_ref: ZipFile FRE da CVM.
        year: Ano do exercício.

    Returns:
        DataFrame com colunas ``CNPJ_CIA``, ``ANO``, ``QTDE_ACOES`` (total),
        ``QTDE_ON`` (ordinárias) e ``QTDE_PN`` (preferenciais). Vazio se
        o arquivo não existir ou estiver malformado.

    Note:
        A separação ON/PN é essencial para calcular Market Cap consolidado
        corretamente em empresas com duas classes de ações (Petrobras, Itaú).
        Sem essa separação, multiplicar o total de ações pelo preço de
        apenas uma classe infla o MC artificialmente.
    """
    filename = f"fre_cia_aberta_capital_social_{year}.csv"
    if filename not in zip_ref.namelist():
        return pl.DataFrame()

    df_raw = _read_fre_raw(zip_ref, filename, year)
    if df_raw.is_empty():
        return pl.DataFrame()

    df_typed = _cast_fre_columns(df_raw)
    df_latest = _select_latest_fre_row_per_company(df_typed)
    return _build_fre_output(df_latest, year)


def _read_fre_raw(
    zip_ref: zipfile.ZipFile, filename: str, year: int
) -> pl.DataFrame:
    """Lê o CSV FRE aplicando decodificação latin1 → utf8.

    Retorna DataFrame vazio em caso de erro de I/O ou parsing (estrutura
    do FRE muda entre anos, então falhas isoladas são esperadas).
    """
    try:
        with zip_ref.open(filename) as f:
            clean_bytes = _decode_cvm_bytes(f.read())
        df = pl.read_csv(clean_bytes, separator=";", infer_schema_length=0)
    except (KeyError, zipfile.BadZipFile, pl.exceptions.ComputeError) as exc:
        logger.error("Erro lendo FRE {}: {}", year, exc)
        return pl.DataFrame()

    if df.is_empty() or "Quantidade_Total_Acoes" not in df.columns:
        return pl.DataFrame()
    return df


def _cast_fre_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Aplica casts numéricos nas colunas FRE, tratando ON/PN defensivamente."""
    df = df.with_columns(
        pl.col("CNPJ_Companhia").str.strip_chars().alias("CNPJ_CIA"),
        pl.col("Versao").cast(pl.Int32),
        pl.col("Quantidade_Total_Acoes").cast(pl.Float64),
    )

    if "Quantidade_Acoes_Ordinarias" in df.columns:
        df = df.with_columns(
            pl.col("Quantidade_Acoes_Ordinarias").cast(pl.Float64, strict=False)
        )
    if "Quantidade_Acoes_Preferenciais" in df.columns:
        df = df.with_columns(
            pl.col("Quantidade_Acoes_Preferenciais").cast(pl.Float64, strict=False)
        )
    return df


def _select_latest_fre_row_per_company(df: pl.DataFrame) -> pl.DataFrame:
    """Filtra por tipo de capital e mantém a versão mais recente por CNPJ.

    Alguns arquivos FRE não trazem ``Tipo_Capital``; nesse caso ordenamos
    apenas pela versão decrescente.
    """
    if "Tipo_Capital" in df.columns:
        df = df.filter(pl.col("Tipo_Capital").is_in(_FRE_CAPITAL_TYPES))
        df = df.sort(
            ["CNPJ_CIA", "Versao", "Tipo_Capital"],
            descending=[False, True, True],
        )
    else:
        df = df.sort(["CNPJ_CIA", "Versao"], descending=[False, True])

    return df.unique(subset=["CNPJ_CIA"], keep="first")


def _build_fre_output(df: pl.DataFrame, year: int) -> pl.DataFrame:
    """Seleciona as colunas finais do FRE com defaults para ON/PN ausentes."""
    has_on = "Quantidade_Acoes_Ordinarias" in df.columns
    has_pn = "Quantidade_Acoes_Preferenciais" in df.columns

    qtde_on = (
        pl.col("Quantidade_Acoes_Ordinarias")
        if has_on
        else pl.lit(None, dtype=pl.Float64)
    )
    qtde_pn = (
        pl.col("Quantidade_Acoes_Preferenciais")
        if has_pn
        else pl.lit(None, dtype=pl.Float64)
    )

    return df.select(
        [
            pl.col("CNPJ_CIA"),
            pl.lit(year).alias("ANO"),
            pl.col("Quantidade_Total_Acoes").alias("QTDE_ACOES"),
            qtde_on.alias("QTDE_ON"),
            qtde_pn.alias("QTDE_PN"),
        ]
    )


# --- API pública: histórico consolidado em Parquet ---


def load_parquet_history(
    file_paths: Sequence[str | pathlib.Path],
    filter_cnpjs: Sequence[str] | None = None,
) -> pl.DataFrame:
    """Carrega histórico de múltiplos Parquets via Lazy Scan.

    Args:
        file_paths: Caminhos dos Parquets (strings ou ``Path``).
        filter_cnpjs: Se fornecido, filtra apenas esses CNPJs
            (aproveita predicate pushdown do Polars).

    Returns:
        DataFrame consolidado. Vazio se ``file_paths`` estiver vazio.

    Optimizations:
        - ``scan_parquet(low_memory=True)`` para datasets grandes.
        - ``filter`` antes do ``collect`` para aproveitar predicate pushdown.
    """
    if not file_paths:
        return pl.DataFrame()

    paths_str: list[str] = [str(p) for p in file_paths]
    lf = pl.scan_parquet(paths_str, low_memory=True)

    if filter_cnpjs is not None:
        lf = lf.filter(pl.col("CNPJ_CIA").is_in(list(filter_cnpjs)))

    return lf.collect()


# --- API pública: cadastro de setores ---


def get_sectors(cadastro_url: str, cache_dir: str = ".synetra_cache") -> pl.DataFrame:
    """Baixa e faz cache do cadastro de setores econômicos da CVM.

    Args:
        cadastro_url: URL do CSV do cadastro CVM.
        cache_dir: Diretório onde salvar o Parquet em cache.

    Returns:
        DataFrame com colunas ``CNPJ_CIA`` e ``SETOR_ATIV``. Vazio em caso de
        erro de rede (permite o pipeline continuar sem classificação setorial).
    """
    cad_cache_file = pathlib.Path(cache_dir) / "cad_cia_aberta.parquet"

    if cad_cache_file.exists():
        return pl.read_parquet(cad_cache_file)

    return _download_and_cache_sectors(cadastro_url, cad_cache_file)


def _download_and_cache_sectors(
    cadastro_url: str, cache_file: pathlib.Path
) -> pl.DataFrame:
    """Baixa cadastro setorial e persiste em Parquet zstd.

    Retorna DataFrame vazio em falhas de rede ou parsing (bloqueios reais
    — erros de programação sobem como exceção). Não tenta se recuperar de
    diretório de cache inválido — o chamador controla isso.
    """
    logger.info("Baixando cadastro CVM pela primeira vez...")
    try:
        df_cad = pl.read_csv(
            cadastro_url,
            separator=";",
            encoding="latin1",
            infer_schema_length=0,
        )
    except (pl.exceptions.ComputeError, urllib.error.URLError, OSError) as exc:
        logger.warning(
            "Erro ao obter setores: {}. Prosseguindo sem classificação.", exc
        )
        return pl.DataFrame()

    df_setor = df_cad.select(["CNPJ_CIA", "SETOR_ATIV"]).unique()

    cache_file.parent.mkdir(exist_ok=True)
    df_setor.write_parquet(cache_file, compression="zstd")
    return df_setor
