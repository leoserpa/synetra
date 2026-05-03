"""
Responsabilidade 3: Transformações e cálculos de indicadores financeiros.
Funções puras que recebem DataFrames e retornam DataFrames processados.
Se você já tem os dados e não quer baixar, pode usar este módulo diretamente.
"""
import logging
import polars as pl
from synetra.utils import clean_text_expr

logger = logging.getLogger("synetra.transformer")


# ============================================================
# Funções Auxiliares
# ============================================================

def safe_div(num: str, den: str) -> pl.Expr:
    """Divisão segura que retorna None quando o denominador é zero."""
    return pl.col(num) / pl.when(pl.col(den) == 0).then(None).otherwise(pl.col(den))


def classify_sectors(df: pl.DataFrame, config: dict) -> pl.DataFrame:
    """
    Classifica empresas em INDUSTRIAL, FINANCEIRO ou SEGURADORA
    com base nas palavras-chave do TOML.
    """
    kw_fin = "|".join(config["setores"]["financeiro"])
    kw_seg = "|".join(config["setores"]["seguradora"])

    return df.with_columns(
        pl.when(pl.col('SETOR_ATIV').str.to_uppercase().str.contains(kw_fin))
        .then(pl.lit('FINANCEIRO'))
        .when(pl.col('SETOR_ATIV').str.to_uppercase().str.contains(kw_seg))
        .then(pl.lit('SEGURADORA'))
        .otherwise(pl.lit('INDUSTRIAL')).alias('CATEGORIA')
    )


def map_accounts(df: pl.DataFrame, config: dict) -> pl.DataFrame:
    """
    Mapeia códigos de contas contábeis para nomes padronizados,
    por setor, usando Single-Join Global (uma única operação).

    OTIMIZAÇÃO: Em vez de 3 filtros + 3 joins + 1 concat (O(3N)),
    construímos um único DataFrame de lookup com chave composta
    [CATEGORIA, CD_CONTA] e fazemos apenas 1 join (O(N)).
    """
    # Construir lookup unificado: [CATEGORIA, CD_CONTA, CONTA_MAP]
    lookup_rows = []
    sector_map = {
        "industrial": "INDUSTRIAL",
        "financeiro": "FINANCEIRO",
        "seguradora": "SEGURADORA",
    }
    for setor_key, categoria_val in sector_map.items():
        for cd_conta, conta_map in config["contas"][setor_key].items():
            lookup_rows.append({"CATEGORIA": categoria_val, "CD_CONTA": cd_conta, "CONTA_MAP": conta_map})

    lookup_df = pl.DataFrame(lookup_rows)

    # Single-Join Global: chave composta [CATEGORIA, CD_CONTA]
    df_all = df.join(lookup_df, on=["CATEGORIA", "CD_CONTA"], how="left")

    # Guarda defensiva: garante que CONTA_NOME existe antes de combinar
    if "CONTA_NOME" not in df_all.columns:
        df_all = df_all.with_columns(pl.lit(None).cast(pl.Utf8).alias("CONTA_NOME"))
    return df_all.with_columns(
        pl.when(pl.col("CONTA_NOME").is_null())
        .then(pl.col("CONTA_MAP"))
        .otherwise(pl.col("CONTA_NOME"))
        .alias("CONTA_NOME")
    )


# ============================================================
# Motor Principal de Transformação
# ============================================================

class FinancialTransformer:
    """
    Motor de cálculo de indicadores financeiros.
    Recebe DataFrames Polars genéricos e retorna indicadores calculados.
    """

    def __init__(self, config: dict):
        self.config = config

    def enrich_with_sectors(self, df_history: pl.DataFrame) -> pl.DataFrame:
        """Adiciona classificação setorial ao histórico CVM."""
        logger.info("Obtendo Setores Econômicos...")
        import pathlib
        cache_dir = pathlib.Path(".synetra_cache")
        cache_dir.mkdir(exist_ok=True)
        cad_cache_file = cache_dir / "cad_cia_aberta.parquet"

        try:
            if cad_cache_file.exists():
                df_setor = pl.read_parquet(cad_cache_file)
            else:
                logger.info("Baixando arquivo de cadastro CVM pela primeira vez...")
                url_cad = self.config["urls"]["cadastro"]
                df_cad = pl.read_csv(url_cad, separator=';', encoding='latin1', infer_schema_length=0)
                df_setor = df_cad.select(['CNPJ_CIA', 'SETOR_ATIV']).unique()
                df_setor.write_parquet(cad_cache_file, compression="zstd")
        except Exception as e:
            logger.warning("Erro ao obter setores da CVM: %s", e)
            df_setor = pl.DataFrame({'CNPJ_CIA': [], 'SETOR_ATIV': []}, schema={'CNPJ_CIA': pl.Utf8, 'SETOR_ATIV': pl.Utf8})

        # Limpeza nativa do setor (sem map_elements)
        df_setor = df_setor.with_columns(clean_text_expr('SETOR_ATIV').alias('SETOR_ATIV'))

        df_history = df_history.join(df_setor, on='CNPJ_CIA', how='left').with_columns(
            pl.col('SETOR_ATIV').fill_null('NAO INFORMADO')
        )

        return classify_sectors(df_history, self.config)

    def detect_special_accounts(self, df: pl.DataFrame) -> pl.DataFrame:
        """Detecta contas especiais (Depreciação, CAPEX, Dividendos) por descrição."""
        # Limpeza nativa de DS_CONTA (sem map_elements)
        df = df.with_columns(clean_text_expr('DS_CONTA').alias('DS_NORM'))

        return df.with_columns(
            pl.when(pl.col('CD_CONTA').str.starts_with('6.01') & pl.col('DS_NORM').str.contains('DEPRECIA|AMORTIZA'))
            .then(pl.lit('DEPREC_AMORT'))
            .when(pl.col('CD_CONTA').str.starts_with('6.02') & pl.col('DS_NORM').str.contains('AQUISIC|ADIC|COMPRA') & pl.col('DS_NORM').str.contains('IMOBILIZADO|INTANGIVEL'))
            .then(pl.lit('CAPEX_VAL'))
            .when(pl.col('CD_CONTA').str.starts_with('6.03') & pl.col('DS_NORM').str.contains('DIVIDENDO|JURO SOBRE CAPITAL|JUROS SOBRE CAPITAL|JURO S CAPITAL|JUROS S CAPITAL|JCP'))
            .then(pl.lit('DIVIDENDOS_PAGOS'))
            .when((pl.col('CATEGORIA') == 'SEGURADORA') & pl.col('DS_NORM').str.contains('RESULTADO ANTES DO RESULTADO FINANCEIRO|RESULTADO ANTES DAS RECEITAS'))
            .then(pl.lit('EBIT'))
            .otherwise(pl.lit(None)).alias('CONTA_NOME')
        )

    def calculate_indicators(self, df_history: pl.DataFrame, df_matches: pl.DataFrame, df_fre_history: pl.DataFrame = None) -> pl.DataFrame:
        """
        Motor principal de cálculo vetorizado.
        Aceita DataFrames Polars de qualquer origem (não precisa ser do downloader).
        """
        logger.info("Motor Polars Ativado: Iniciando cálculos vetorizados...")

        # Limpeza nativa do RAZAO_CVM (sem map_elements)
        df_matches = df_matches.with_columns(clean_text_expr('RAZAO_CVM').alias('RAZAO_CVM'))

        # 1. Enriquecer com setores
        df_history = self.enrich_with_sectors(df_history)

        # 2. Detectar contas especiais
        df_history = self.detect_special_accounts(df_history)

        # 3. Mapear contas por setor
        df_history = map_accounts(df_history, self.config)

        # 4. Filtrar e pivotar
        df_filtered = df_history.filter(pl.col("CONTA_NOME").is_not_null())
        df_pivot = df_filtered.pivot(
            values="VL_CONTA", index=["CNPJ_CIA", "ANO", "CATEGORIA", "SETOR_ATIV"],
            columns="CONTA_NOME", aggregate_function="sum"
        )

        # 5. Garantir presença de todas as colunas (Single-Evaluation)
        # OTIMIZAÇÃO: Em vez de N chamadas .with_columns() em loop (cada uma
        # alocando um novo nó no plano de execução), coletamos todas as colunas
        # ausentes e injetamos de uma vez. O motor Polars/Rust paraleliza isso.
        todas_contas = set()
        for setor_contas in self.config["contas"].values():
            todas_contas.update(setor_contas.values())
        todas_contas.update(['DEPREC_AMORT', 'CAPEX_VAL', 'DIVIDENDOS_PAGOS', 'APLICACOES_CP', 'APLICACOES_LP'])

        missing_cols = [c for c in todas_contas if c not in df_pivot.columns]
        if missing_cols:
            df_pivot = df_pivot.with_columns([pl.lit(0.0).alias(c) for c in missing_cols])

        df_pivot = df_pivot.fill_null(value=0.0)

        # 6. Consolidações setoriais
        df_pivot = df_pivot.with_columns(
            pl.when(pl.col('CATEGORIA') == 'SEGURADORA')
            .then(pl.col('CAIXA_EQUIVALENTES') + pl.col('APLICACOES_CP') + pl.col('APLICACOES_LP'))
            .otherwise(pl.col('CAIXA_EQUIVALENTES'))
            .alias('CAIXA_EQUIVALENTES')
        )

        df_pivot = df_pivot.with_columns(
            pl.when(pl.col('LUCRO_LIQUIDO') != 0)
            .then(pl.col('LUCRO_LIQUIDO'))
            .otherwise(pl.col('LUCRO_LIQUIDO_BCO'))
            .alias('LUCRO_FINAL')
        )

        # 7. Merge FRE (Quantidade de Ações)
        df_pivot = df_pivot.with_columns(pl.col('ANO').cast(pl.Int32))
        if df_fre_history is not None and not df_fre_history.is_empty():
            df_fre_history = df_fre_history.with_columns(pl.col('ANO').cast(pl.Int32))
            df_pivot = df_pivot.join(df_fre_history, on=['CNPJ_CIA', 'ANO'], how='left')
        else:
            df_pivot = df_pivot.with_columns(pl.lit(None).cast(pl.Float64).alias('QTDE_ACOES'))

        # 8. Cálculo de indicadores (100% vetorizado)
        df_pivot = df_pivot.with_columns([
            safe_div('LUCRO_FINAL', 'PATRIMONIO_LIQUIDO').alias('ROE'),
            safe_div('LUCRO_FINAL', 'QTDE_ACOES').alias('LPA'),
            safe_div('PATRIMONIO_LIQUIDO', 'QTDE_ACOES').alias('VPA'),
            safe_div('EBIT', 'RECEITA_LIQUIDA').alias('MARGEM_EBIT'),
            (pl.col('LUCRO_FINAL') - pl.col('FCO')).alias('ACCRUALS'),
            pl.when(pl.col('CATEGORIA') == 'FINANCEIRO').then(None).otherwise(pl.col('CAPEX_VAL')).alias('CAPEX'),
            pl.col('DIVIDENDOS_PAGOS').abs().alias('PROVENTOS')
        ]).with_columns([
            safe_div('LUCRO_FINAL', 'RECEITA_LIQUIDA').alias('MARGEM_LIQUIDA'),
            safe_div('RESULTADO_BRUTO', 'RECEITA_LIQUIDA').alias('MARGEM_BRUTA'),
            safe_div('LUCRO_FINAL', 'ATIVO_TOTAL').alias('ROA'),
            safe_div('RECEITA_LIQUIDA', 'ATIVO_TOTAL').alias('GIRO_ATIVO'),
            safe_div('DIVIDA_LP', 'ATIVO_TOTAL').alias('ALAVANCAGEM_LP'),
            pl.when(pl.col('CATEGORIA') == 'FINANCEIRO').then(None).otherwise(pl.col('FCO') + pl.col('CAPEX')).alias('FCL'),
            safe_div('PROVENTOS', 'LUCRO_FINAL').alias('PAYOUT'),
            pl.when(pl.col('CATEGORIA') == 'FINANCEIRO').then(None).otherwise(pl.col('DEPREC_AMORT')).alias('DEPREC_AMORT')
        ]).with_columns([
            pl.when(pl.col('CATEGORIA').is_in(['INDUSTRIAL', 'SEGURADORA']))
            .then(pl.col('EBIT') + pl.col('DEPREC_AMORT'))
            .otherwise(None).alias('EBITDA'),
        ]).with_columns([
            pl.when(pl.col('CATEGORIA').is_in(['INDUSTRIAL', 'SEGURADORA']))
            .then(safe_div('EBITDA', 'RECEITA_LIQUIDA'))
            .otherwise(None).alias('MARGEM_EBITDA'),
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL')
            .then(pl.col('DIVIDA_CP') + pl.col('DIVIDA_LP'))
            .otherwise(pl.when(pl.col('CATEGORIA') == 'SEGURADORA').then(pl.lit(0.0)).otherwise(None))
            .alias('DIVIDA_TOTAL'),
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL')
            .then(safe_div('ATIVO_CIRCULANTE', 'PASSIVO_CIRCULANTE'))
            .otherwise(None).alias('LIQUIDEZ_CORRENTE')
        ]).with_columns([
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL')
            .then(pl.col('DIVIDA_TOTAL') - pl.col('CAIXA_EQUIVALENTES'))
            .when(pl.col('CATEGORIA') == 'SEGURADORA')
            .then(pl.lit(0.0) - pl.col('CAIXA_EQUIVALENTES'))
            .otherwise(None).alias('DIVIDA_LIQUIDA'),
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL')
            .then(safe_div('DIVIDA_TOTAL', 'PATRIMONIO_LIQUIDO'))
            .when(pl.col('CATEGORIA') == 'SEGURADORA')
            .then(pl.lit(0.0))
            .otherwise(None).alias('DIVIDA_PL')
        ]).with_columns([
            pl.when(pl.col('CATEGORIA').is_in(['INDUSTRIAL', 'SEGURADORA']))
            .then(safe_div('DIVIDA_LIQUIDA', 'EBITDA'))
            .otherwise(None).alias('DL_EBITDA'),
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL').then(
                (pl.col('EBIT') * 0.66) / pl.when((pl.col('PATRIMONIO_LIQUIDO') + pl.col('DIVIDA_TOTAL')) == 0)
                .then(None).otherwise(pl.col('PATRIMONIO_LIQUIDO') + pl.col('DIVIDA_TOTAL'))
            ).otherwise(None).alias('ROIC')
        ])

        # 9. Merge com mapa de Tickers
        df_final = df_matches.join(df_pivot, on='CNPJ_CIA', how='inner')
        df_final = df_final.rename({'RESULTADO_BRUTO': 'LUCRO_BRUTO', 'DIVIDA_TOTAL': 'DIVIDA_BRUTA'})
        if 'LUCRO_LIQUIDO' in df_final.columns:
            df_final = df_final.drop('LUCRO_LIQUIDO')

        # 10. Organizar colunas
        cols_id = ['TICKER', 'ANO', 'CATEGORIA', 'SETOR_ATIV', 'RAZAO_CVM', 'CNPJ_CIA']
        cols_fin = [
            'QTDE_ACOES', 'RECEITA_LIQUIDA', 'LUCRO_BRUTO', 'EBIT', 'EBITDA', 'DEPREC_AMORT',
            'LUCRO_FINAL', 'FCO', 'CAPEX', 'FCL', 'PROVENTOS', 'PAYOUT',
            'ATIVO_TOTAL', 'ATIVO_CIRCULANTE', 'PASSIVO_CIRCULANTE', 'PATRIMONIO_LIQUIDO',
            'CAIXA_EQUIVALENTES', 'DIVIDA_BRUTA', 'DIVIDA_LIQUIDA', 'DL_EBITDA', 'DIVIDA_PL',
            'ROE', 'ROA', 'ROIC', 'MARGEM_BRUTA', 'MARGEM_EBIT', 'MARGEM_EBITDA', 'MARGEM_LIQUIDA',
            'LIQUIDEZ_CORRENTE', 'ACCRUALS', 'LPA', 'VPA', 'GIRO_ATIVO', 'ALAVANCAGEM_LP'
        ]
        cols_fin = [c for c in cols_fin if c in df_final.columns]
        df_wide = df_final.select(cols_id + cols_fin)
        df_wide = df_wide.with_columns(pl.col('ANO').cast(pl.Int32)).sort(['CATEGORIA', 'TICKER', 'ANO'])

        # 11. Piotroski F-Score (Industrial)
        # OTIMIZAÇÃO: Expressão idiomática Polars — uma única lista de colunas
        # com .shift(1).over() e .name.suffix(), delegando ao motor Rust.
        _fscore_cols = ['ROA', 'ALAVANCAGEM_LP', 'LIQUIDEZ_CORRENTE', 'QTDE_ACOES', 'MARGEM_BRUTA', 'GIRO_ATIVO']
        df_wide = df_wide.with_columns(
            [pl.col(c).shift(1).over('TICKER').alias(f'{c}_prev') for c in _fscore_cols]
        )

        df_wide = df_wide.with_columns(
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL').then(
                pl.when(pl.col('ROA_prev').is_not_null()).then(
                    (pl.col('ROA') > 0).cast(pl.Int32).fill_null(0) +
                    (pl.col('FCO') > 0).cast(pl.Int32).fill_null(0) +
                    (pl.col('ROA') > pl.col('ROA_prev')).cast(pl.Int32).fill_null(0) +
                    (pl.col('FCO') > pl.col('LUCRO_FINAL')).cast(pl.Int32).fill_null(0) +
                    (pl.col('ALAVANCAGEM_LP') < pl.col('ALAVANCAGEM_LP_prev')).cast(pl.Int32).fill_null(0) +
                    (pl.col('LIQUIDEZ_CORRENTE') > pl.col('LIQUIDEZ_CORRENTE_prev')).cast(pl.Int32).fill_null(0) +
                    (pl.col('QTDE_ACOES') <= pl.col('QTDE_ACOES_prev')).cast(pl.Int32).fill_null(0) +
                    (pl.col('MARGEM_BRUTA') > pl.col('MARGEM_BRUTA_prev')).cast(pl.Int32).fill_null(0) +
                    (pl.col('GIRO_ATIVO') > pl.col('GIRO_ATIVO_prev')).cast(pl.Int32).fill_null(0)
                ).otherwise(None)
            ).otherwise(None).alias('F_SCORE')
        )

        df_wide = df_wide.drop([f'{c}_prev' for c in _fscore_cols] + ['ALAVANCAGEM_LP'])

        # 12. Arredondamentos
        cols_4d = ['ROE', 'ROA', 'ROIC', 'MARGEM_BRUTA', 'MARGEM_EBIT', 'MARGEM_EBITDA',
                   'MARGEM_LIQUIDA', 'LIQUIDEZ_CORRENTE', 'DL_EBITDA', 'DIVIDA_PL', 'PAYOUT', 'LPA', 'VPA']
        cols_2d = ['RECEITA_LIQUIDA', 'LUCRO_BRUTO', 'LUCRO_FINAL', 'DIVIDA_LIQUIDA',
                   'EBITDA', 'FCO', 'CAPEX', 'FCL', 'DEPREC_AMORT', 'DIVIDA_BRUTA']

        df_wide = df_wide.with_columns([
            pl.col(c).round(4) for c in cols_4d if c in df_wide.columns
        ]).with_columns([
            pl.col(c).round(2) for c in cols_2d if c in df_wide.columns
        ])

        logger.info("Cálculos finalizados: %d linhas, %d colunas.", df_wide.height, df_wide.width)
        df_wide = df_wide.rename({'LUCRO_FINAL': 'LUCRO_LIQUIDO'})
        return df_wide
