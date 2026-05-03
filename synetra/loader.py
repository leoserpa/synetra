"""Leitura de arquivos CSV/Parquet e validação de schema."""
import logging
import polars as pl

logger = logging.getLogger("synetra.loader")

# Colunas obrigatórias que todo DataFrame CVM deve ter
REQUIRED_COLUMNS = ['CD_CONTA', 'CNPJ_CIA', 'VL_CONTA', 'ORDEM_EXERC', 'DS_CONTA']


def read_cvm_csv(zip_ref, filename: str) -> pl.DataFrame:
    """
    Responsabilidade 2: Lê um CSV dentro de um ZIP e retorna um DataFrame Polars
    com tratamento mínimo (schema e limpeza de espaços).
    """
    try:
        with zip_ref.open(filename) as f:
            df = pl.read_csv(
                f.read(),
                separator=';',
                encoding='latin1',
                infer_schema_length=0,
                null_values=['', 'NA'],
                quote_char=None
            )
            # Validação de colunas obrigatórias
            missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
            if missing:
                logger.warning("Colunas faltando em %s: %s", filename, missing)

            # Limpeza mínima de espaços
            df = df.with_columns([
                pl.col('CD_CONTA').str.strip_chars(),
                pl.col('CNPJ_CIA').str.strip_chars()
            ])
            return df
    except KeyError:
        return pl.DataFrame()


def process_year_from_zip(zip_ref, year: int, doc_types: list) -> pl.DataFrame:
    """
    Lê e consolida todos os documentos de um ano a partir de um ZipFile.
    Aplica regras de dedup (CON > IND, DFC_MI > DFC_MD) e ajuste de escala.
    """
    df_year = []
    for doc_type in doc_types:
        dfs_to_concat = []

        # Consolidado (prioridade 1)
        df_con = read_cvm_csv(zip_ref, f"dfp_cia_aberta_{doc_type}_con_{year}.csv")
        if not df_con.is_empty():
            df_con = df_con.with_columns(pl.lit(1).alias('PRIORIDADE_TIPO'))
            dfs_to_concat.append(df_con)

        # Individual (prioridade 2)
        df_ind = read_cvm_csv(zip_ref, f"dfp_cia_aberta_{doc_type}_ind_{year}.csv")
        if not df_ind.is_empty():
            df_ind = df_ind.with_columns(pl.lit(2).alias('PRIORIDADE_TIPO'))
            dfs_to_concat.append(df_ind)

        if dfs_to_concat:
            df_combined = pl.concat(dfs_to_concat, how="diagonal_relaxed")
            df_combined = df_combined.with_columns(
                pl.lit('DFC' if 'DFC' in doc_type else doc_type).alias('DOC_TYPE'),
                pl.lit(1 if doc_type == 'DFC_MI' else (2 if doc_type == 'DFC_MD' else 0)).alias('PRIORIDADE_DFC'),
                pl.lit(year).alias('ANO')
            )
            df_year.append(df_combined)

    if not df_year:
        logger.warning("Nenhum dado valido encontrado para o ano %d", year)
        return pl.DataFrame()

    # Concatena e aplica regras de dedup
    df_consolidated = pl.concat(df_year, how="diagonal_relaxed")
    df_consolidated = df_consolidated.filter(pl.col('ORDEM_EXERC') == 'ÚLTIMO')

    sort_cols = ['CNPJ_CIA', 'CD_CONTA', 'PRIORIDADE_TIPO', 'PRIORIDADE_DFC']
    sort_desc = [False, False, False, False]
    if 'VERSAO' in df_consolidated.columns:
        sort_cols.append('VERSAO')
        sort_desc.append(True)

    df_consolidated = df_consolidated.sort(sort_cols, descending=sort_desc)
    df_consolidated = df_consolidated.unique(subset=['CNPJ_CIA', 'CD_CONTA'], keep='first')

    drop_cols = [c for c in ['PRIORIDADE_DFC', 'PRIORIDADE_TIPO'] if c in df_consolidated.columns]
    if drop_cols:
        df_consolidated = df_consolidated.drop(drop_cols)

    # Ajuste de Escala (MIL -> unidade)
    df_consolidated = df_consolidated.with_columns(
        pl.col('VL_CONTA').cast(pl.Float64, strict=False)
    ).with_columns(
        pl.when(pl.col('ESCALA_MOEDA').str.to_uppercase().str.contains('MIL'))
        .then(pl.col('VL_CONTA') * 1000)
        .otherwise(pl.col('VL_CONTA'))
        .alias('VL_CONTA')
    )

    return df_consolidated


def process_fre_from_zip(zip_ref, year: int) -> pl.DataFrame:
    """Lê dados de quantidade de ações (FRE) a partir de um ZipFile."""
    filename = f"fre_cia_aberta_capital_social_{year}.csv"
    if filename not in zip_ref.namelist():
        return pl.DataFrame()

    try:
        with zip_ref.open(filename) as f:
            df = pl.read_csv(f.read(), separator=';', encoding='latin1', infer_schema_length=0)

            if df.is_empty():
                return pl.DataFrame()

            df = df.with_columns([
                pl.col('CNPJ_Companhia').str.strip_chars().alias('CNPJ_CIA'),
                pl.col('Versao').cast(pl.Int32)
            ])

            if 'Quantidade_Total_Acoes' not in df.columns:
                return pl.DataFrame()

            df = df.with_columns(pl.col('Quantidade_Total_Acoes').cast(pl.Float64))

            if 'Tipo_Capital' in df.columns:
                df = df.filter(pl.col('Tipo_Capital').is_in(['Capital Integralizado', 'Capital Emitido']))
                df = df.sort(['CNPJ_CIA', 'Versao', 'Tipo_Capital'], descending=[False, True, True])
            else:
                df = df.sort(['CNPJ_CIA', 'Versao'], descending=[False, True])

            df = df.unique(subset=['CNPJ_CIA'], keep='first')
            df = df.select([
                pl.col('CNPJ_CIA'),
                pl.lit(year).alias('ANO'),
                pl.col('Quantidade_Total_Acoes').alias('QTDE_ACOES')
            ])
            return df
    except Exception as e:
        logger.error("Erro processando FRE %d: %s", year, e)
        return pl.DataFrame()


def load_parquet_history(file_paths: list, filter_cnpjs: list = None) -> pl.DataFrame:
    """
    Carrega histórico a partir de múltiplos arquivos Parquet via Lazy Scan.
    Aplica pushdown predicate se filter_cnpjs for informado.
    """
    if not file_paths:
        return pl.DataFrame()

    lf = pl.scan_parquet(file_paths)
    if filter_cnpjs is not None:
        lf = lf.filter(pl.col('CNPJ_CIA').is_in(filter_cnpjs))
    return lf.collect()
