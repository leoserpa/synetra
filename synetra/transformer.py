"""Transformações e cálculos de indicadores financeiros (funções puras).

Arquitetura em 3 camadas:

    1. **Funções de módulo** (``classify_sectors``, ``map_accounts``):
       helpers puros reutilizáveis em testes e por outros módulos.

    2. **Constantes do pipeline**: agrupadas por responsabilidade
       (core, ordenação, arredondamento, assepsia setorial).

    3. **``FinancialTransformer``**: orquestra os passos privados em
       ``calculate_indicators``. Cada passo pode ser testado ou
       substituído isoladamente.

Fluxo de ``calculate_indicators``::

    1. _prepare_history           → enriquece com setores + mapeia contas
    2. _pivot_and_consolidate     → pivota + consolidações setoriais + FRE
    3. calculate_all_indicators   → Tiers 1-5 de fórmulas puras (domain)
    4. _merge_tickers             → junta com Ticker↔CNPJ + seleção
    5. _apply_sector_assepsia     → anula métricas industriais em bancos
    6. _prepare_shifts            → gera colunas _prev/_BASE num único passo
    7. _calculate_fscore          → Piotroski F-Score (9 critérios)
    8. _calculate_beneish         → Beneish M-Score (anti-fraude)
    9. _calculate_growth_and_quant→ YoY + CAGR + 5 fatores quantitativos
    10. _calculate_efficiency_and_quality → 10 indicadores de eficiência
    11. _round_and_finalize       → cleanup + arredondamentos + rename
"""
from __future__ import annotations

import polars as pl
from loguru import logger

from synetra.config import SynetraConfig
from synetra.domain import BRAZIL_AFTER_TAX, Categoria, calculate_all_indicators, safe_div
from synetra.utils import clean_text_expr

# --- Funções puras de nível de módulo (sem acesso a self) ---


def classify_sectors(df: pl.DataFrame, config: SynetraConfig) -> pl.DataFrame:
    """Classifica empresas em INDUSTRIAL, FINANCEIRO ou SEGURADORA via keywords."""
    kw_fin = "(?i)" + "|".join(config.setores.financeiro)
    kw_seg = "(?i)" + "|".join(config.setores.seguradora)

    return df.with_columns(
        pl.when(pl.col("SETOR_ATIV").str.contains(kw_fin))
        .then(pl.lit(Categoria.FINANCEIRO.value))
        .when(pl.col("SETOR_ATIV").str.contains(kw_seg))
        .then(pl.lit(Categoria.SEGURADORA.value))
        .otherwise(pl.lit(Categoria.INDUSTRIAL.value))
        .alias("CATEGORIA")
    )


def map_accounts(df: pl.DataFrame, config: SynetraConfig) -> pl.DataFrame:
    """Mapeia códigos de contas contábeis para nomes padronizados.

    Constrói um lookup ``(CATEGORIA, CD_CONTA) → CONTA_NOME`` como DataFrame
    e resolve tudo com um único join, sem loops Python sobre linhas.
    """
    sector_map = {
        "industrial": Categoria.INDUSTRIAL.value,
        "financeiro": Categoria.FINANCEIRO.value,
        "seguradora": Categoria.SEGURADORA.value,
    }
    lookup_rows = [
        {"CATEGORIA": categoria_val, "CD_CONTA": cd_conta, "CONTA_MAP": conta_map}
        for setor_key, categoria_val in sector_map.items()
        for cd_conta, conta_map in config.contas[setor_key].items()
    ]

    lookup_df = pl.DataFrame(lookup_rows)
    df_all = df.join(lookup_df, on=["CATEGORIA", "CD_CONTA"], how="left")

    if "CONTA_NOME" not in df_all.columns:
        df_all = df_all.with_columns(pl.lit(None, dtype=pl.Utf8).alias("CONTA_NOME"))

    return df_all.with_columns(
        pl.when(pl.col("CONTA_NOME").is_null())
        .then(pl.col("CONTA_MAP"))
        .otherwise(pl.col("CONTA_NOME"))
        .alias("CONTA_NOME")
    )


# --- Constantes do Pipeline ---

# --- Estrutura de dados da CVM -------------------------------

_CORE_HISTORY_COLS = ["CNPJ_CIA", "ANO", "CD_CONTA", "VL_CONTA", "DS_CONTA"]
"""Colunas nucleares preservadas após projection pushdown."""

# --- F-Score e Beneish: colunas que precisam de shift -------

_FSCORE_SHIFT_COLS = [
    "ROA", "ALAVANCAGEM_LP", "LIQUIDEZ_CORRENTE",
    "QTDE_ACOES", "MARGEM_BRUTA", "GIRO_ATIVO",
]
"""Colunas com shift(1) usadas no cálculo do Piotroski F-Score."""

_BENEISH_SHIFT_COLS = [
    "CONTAS_A_RECEBER", "RECEITA_LIQUIDA", "MARGEM_BRUTA",
    "ATIVO_CIRCULANTE", "IMOBILIZADO", "ATIVO_TOTAL",
    "DEPREC_AMORT", "DESPESAS_OPERACIONAIS", "DIVIDA_BRUTA",
]
"""Colunas com shift(1) usadas no cálculo do Beneish M-Score."""

# --- Assepsia setorial: quais métricas anular por setor -----

_METRICAS_ANULAR_BANCOS = [
    "EBITDA", "MARGEM_EBITDA", "MARGEM_EBIT", "MARGEM_BRUTA",
    "CAPEX", "DEPREC_AMORT", "ROIC", "LIQUIDEZ_CORRENTE",
    "DIVIDA_BRUTA", "DIVIDA_LIQUIDA", "DL_EBITDA", "DIVIDA_PL",
    "ATIVO_CIRCULANTE", "ATIVO_NAO_CIRCULANTE", "CONTAS_A_RECEBER",
]
"""Métricas industriais anuladas para bancos (FCO distorcido por captação)."""

_METRICAS_ANULAR_SEGURADORAS = [
    "MARGEM_BRUTA", "ROIC", "LIQUIDEZ_CORRENTE",
    "DL_EBITDA", "DIVIDA_PL", "DIVIDA_BRUTA", "DIVIDA_LIQUIDA",
]
"""Métricas anuladas para seguradoras (ruído financeiro industrial)."""

_EFICIENCIA_ANULAR_BANCOS = [
    "MARGEM_FCO", "MARGEM_FCL", "CASH_ROA", "PMR", "CAPITAL_DE_GIRO",
    "ROCE", "NOPAT", "REINVESTMENT_RATE", "CASH_RATIO",
]
"""Métricas de eficiência (Camada 1) anuladas para bancos.

Nota: ``SUSTAINABLE_GROWTH`` é mantido para todos os setores pois
depende apenas de ROE e Payout (imune a distorções setoriais).
"""

# --- Ordem final de colunas ---------------------------------

_COLS_ID = ["TICKER", "ANO", "CATEGORIA", "SETOR_ATIV", "RAZAO_CVM", "CNPJ_CIA"]
"""Colunas identificadoras no output final."""

_COLS_FIN = [
    "QTDE_ACOES", "QTDE_ON", "QTDE_PN", "RECEITA_LIQUIDA", "LUCRO_BRUTO",
    "EBIT", "EBITDA", "DEPREC_AMORT", "LUCRO_FINAL", "FCO", "FCI", "FCF",
    "CAPEX", "FCL", "PROVENTOS", "PAYOUT", "ATIVO_TOTAL", "ATIVO_CIRCULANTE",
    "PASSIVO_CIRCULANTE", "PATRIMONIO_LIQUIDO", "CAIXA_EQUIVALENTES",
    "DIVIDA_BRUTA", "DIVIDA_LIQUIDA", "DL_EBITDA", "DIVIDA_PL",
    "CONTAS_A_RECEBER", "ATIVO_NAO_CIRCULANTE", "IMOBILIZADO",
    "DESPESAS_OPERACIONAIS", "ROE", "ROA", "ROIC", "GP_A",
    "MARGEM_BRUTA", "MARGEM_EBIT", "MARGEM_EBITDA", "MARGEM_LIQUIDA",
    "LIQUIDEZ_CORRENTE", "ACCRUALS", "ACCRUAL_RATIO", "LPA", "VPA",
    "GIRO_ATIVO", "ALAVANCAGEM_LP", "ALTMAN_Z",
]
"""Colunas financeiras no output final (ordem)."""

# --- Arredondamentos ---------------------------------------

_COLS_ROUND_4D = [
    "ROE", "ROA", "ROIC", "GP_A", "MARGEM_BRUTA", "MARGEM_EBIT",
    "MARGEM_EBITDA", "MARGEM_LIQUIDA", "LIQUIDEZ_CORRENTE", "DL_EBITDA",
    "DIVIDA_PL", "PAYOUT", "LPA", "VPA", "ALTMAN_Z", "BENEISH_M",
    "ACCRUAL_RATIO", "CRESC_RECEITA_YOY", "CRESC_LUCRO_YOY",
    "CAGR_RECEITA_3A", "CAGR_RECEITA_5A", "CAGR_LUCRO_3A", "CAGR_LUCRO_5A",
    "CASH_CONVERSION", "EARNINGS_STABILITY", "VOL_LUCRO", "DELTA_ROE",
    "DELTA_MARGEM", "MARGEM_FCO", "MARGEM_FCL", "CASH_ROA", "ROCE",
    "REINVESTMENT_RATE", "SUSTAINABLE_GROWTH", "CASH_RATIO",
]
"""Ratios e percentuais: 4 casas decimais."""

_COLS_ROUND_2D = [
    "RECEITA_LIQUIDA", "LUCRO_BRUTO", "LUCRO_FINAL", "DIVIDA_LIQUIDA",
    "EBITDA", "FCO", "CAPEX", "FCL", "DEPREC_AMORT", "DIVIDA_BRUTA",
    "CAPITAL_DE_GIRO", "NOPAT", "PMR",
]
"""Valores monetários absolutos: 2 casas decimais."""

# --- Constantes de cálculo ---------------------------------

_DIAS_NO_ANO = 365
"""Usado no cálculo do Prazo Médio de Recebimento (PMR)."""


# --- Helpers internos — assepsia setorial ---


def _nullify_for(
    df: pl.DataFrame,
    category: Categoria,
    columns: list[str],
) -> pl.DataFrame:
    """Anula colunas para empresas de uma categoria específica.

    Evita duplicação do padrão ``pl.when(CATEGORIA == X).then(None).otherwise(col)``.

    Args:
        df: DataFrame a ser transformado.
        category: Categoria cujas linhas terão as colunas anuladas.
        columns: Nomes das colunas a anular. Colunas inexistentes são ignoradas.

    Returns:
        DataFrame com as colunas anuladas para a categoria alvo.
    """
    existing = [c for c in columns if c in df.columns]
    if not existing:
        return df
    return df.with_columns(
        [
            pl.when(pl.col("CATEGORIA") == category.value)
            .then(None)
            .otherwise(pl.col(c))
            .alias(c)
            for c in existing
        ]
    )


# --- FinancialTransformer ---


class FinancialTransformer:
    """Calcula indicadores financeiros a partir de dados brutos da CVM.

    Uso::

        transformer = FinancialTransformer(config)
        df_final = transformer.calculate_indicators(
            df_raw, df_matches, df_setor, df_fre_history=df_fre,
        )

    O método ``calculate_indicators`` executa 11 passos privados em
    sequência. Cada passo pode ser modificado sem afetar os demais.
    """

    def __init__(self, config: SynetraConfig) -> None:
        self.config = config

    # --- API Pública ---

    def calculate_indicators(
        self,
        df_history: pl.DataFrame,
        df_matches: pl.DataFrame,
        df_setor: pl.DataFrame,
        df_fre_history: pl.DataFrame | None = None,
    ) -> pl.DataFrame:
        """Calcula indicadores vetorizados (função pura, sem I/O).

        Args:
            df_history: DataFrame cru das contas contábeis da CVM.
            df_matches: Mapa Ticker → CNPJ.
            df_setor: Cadastro de setores econômicos (CNPJ → SETOR_ATIV).
            df_fre_history: Histórico FRE com quantidade de ações (opcional).

        Returns:
            DataFrame largo com ``TICKER × ANO × indicadores`` calculados.
        """
        logger.info("Iniciando cálculo de indicadores.")

        df = self._prepare_history(df_history, df_setor)
        df = self._pivot_and_consolidate(df, df_fre_history)
        df = calculate_all_indicators(df)  # Tiers 1-5 do domain layer
        df = self._merge_tickers(df, df_matches)
        df = self._apply_sector_assepsia(df)
        df = self._prepare_shifts(df)
        df = self._calculate_fscore(df)
        df = self._calculate_beneish(df)
        df = self._calculate_growth_and_quant(df)
        df = self._calculate_efficiency_and_quality(df)
        df = self._round_and_finalize(df)

        logger.info(
            "Cálculo concluído: {} linhas, {} colunas.", df.height, df.width
        )
        return df

    def audit_data(self, df: pl.DataFrame) -> dict:
        """Auditoria de qualidade 100% vetorizada (sem loops Python).

        Returns:
            Dict com chaves: ``gaps_count``, ``tickers_with_gaps``,
            ``roe_outliers``, ``zero_revenue_pct``.
        """
        results: dict = {
            **self._audit_temporal_gaps(df),
            **self._audit_roe_outliers(df),
            **self._audit_zero_revenue(df),
        }
        return results

    @staticmethod
    def _audit_temporal_gaps(df: pl.DataFrame) -> dict:
        """Detecta buracos na série histórica por ticker (anos faltando)."""
        df_gaps = (
            df.select(["TICKER", "ANO"])
            .sort(["TICKER", "ANO"])
            .with_columns(gap=pl.col("ANO").diff().over("TICKER"))
            .filter(pl.col("gap") > 1)
        )
        return {
            "gaps_count": df_gaps.height,
            "tickers_with_gaps": df_gaps.select("TICKER").unique().height,
        }

    @staticmethod
    def _audit_roe_outliers(df: pl.DataFrame) -> dict:
        """Conta registros com |ROE| > 500% (valores claramente aberrantes)."""
        if "ROE" not in df.columns:
            return {}
        return {"roe_outliers": df.filter(pl.col("ROE").abs() > 5.0).height}

    @staticmethod
    def _audit_zero_revenue(df: pl.DataFrame) -> dict:
        """Percentual de registros sem receita (zero ou nulo)."""
        if "RECEITA_LIQUIDA" not in df.columns:
            return {}
        sem_receita = df.filter(
            (pl.col("RECEITA_LIQUIDA") == 0) | pl.col("RECEITA_LIQUIDA").is_null()
        )
        return {"zero_revenue_pct": round(sem_receita.height / max(df.height, 1) * 100, 1)}

    # --- Helpers reutilizáveis (públicos para retrocompatibilidade) ---

    def enrich_with_sectors(
        self, df_history: pl.DataFrame, df_setor: pl.DataFrame
    ) -> pl.DataFrame:
        """Adiciona classificação setorial ao histórico CVM (função pura, sem I/O)."""
        if df_setor.is_empty():
            logger.warning("Dados de setor vazios. Prosseguindo sem classificação.")
            return df_history.with_columns(
                pl.lit("NAO INFORMADO").alias("SETOR_ATIV"),
                pl.lit(Categoria.INDUSTRIAL.value).alias("CATEGORIA"),
            )

        cols_setor = df_setor.columns
        if "CNPJ_CIA" in cols_setor and "SETOR_ATIV" in cols_setor:
            df_setor = df_setor.select(["CNPJ_CIA", "SETOR_ATIV"])

        df_setor = df_setor.with_columns(clean_text_expr("SETOR_ATIV").alias("SETOR_ATIV"))

        df_history = df_history.join(df_setor, on="CNPJ_CIA", how="left").with_columns(
            pl.col("SETOR_ATIV").fill_null("NAO INFORMADO")
        )

        return classify_sectors(df_history, self.config)

    def detect_special_accounts(self, df: pl.DataFrame) -> pl.DataFrame:
        """Detecta contas especiais (Depreciação, CAPEX, Dividendos) via Regex."""
        rx = self.config.regex
        return (
            df.with_columns(clean_text_expr("DS_CONTA").alias("DS_NORM"))
            .with_columns(
                pl.when(
                    pl.col("CD_CONTA").str.starts_with("6.01")
                    & pl.col("DS_NORM").str.contains(rx.depreciacao)
                    & ~pl.col("DS_NORM").str.contains("AJUSTE|JUROS")
                )
                .then(pl.lit("DEPREC_AMORT"))
                .when(
                    pl.col("CD_CONTA").str.starts_with("6.02")
                    & pl.col("DS_NORM").str.contains(rx.capex)
                    & pl.col("DS_NORM").str.contains(rx.capex_tipo)
                )
                .then(pl.lit("CAPEX_VAL"))
                .when(
                    pl.col("CD_CONTA").str.starts_with("6.03")
                    & pl.col("DS_NORM").str.contains(rx.dividendos)
                )
                .then(pl.lit("DIVIDENDOS_PAGOS"))
                .when(
                    (pl.col("CATEGORIA") == Categoria.SEGURADORA.value)
                    & pl.col("DS_NORM").str.contains(rx.ebit_seguradora)
                )
                .then(pl.lit("EBIT"))
                .otherwise(pl.lit(None, dtype=pl.Utf8))
                .alias("CONTA_NOME")
            )
            .drop("DS_NORM")
        )

    # --- Etapas do Pipeline ---

    def _prepare_history(
        self, df_history: pl.DataFrame, df_setor: pl.DataFrame
    ) -> pl.DataFrame:
        """Passo 1: Projection pushdown + enriquecimento setorial + detecção."""
        df_history = df_history.select(
            [c for c in _CORE_HISTORY_COLS if c in df_history.columns]
        )
        df_history = self.enrich_with_sectors(df_history, df_setor)
        df_history = self.detect_special_accounts(df_history)
        df_history = map_accounts(df_history, self.config)
        return df_history

    def _pivot_and_consolidate(
        self,
        df_history: pl.DataFrame,
        df_fre_history: pl.DataFrame | None,
    ) -> pl.DataFrame:
        """Passo 2: Pivot + garantia de colunas + consolidações setoriais + FRE."""
        df_pivot = self._pivot_by_account(df_history)
        df_pivot = self._ensure_all_account_columns(df_pivot)
        df_pivot = self._consolidate_insurance_cash(df_pivot)
        df_pivot = self._resolve_net_income(df_pivot)
        df_pivot = self._merge_shares_history(df_pivot, df_fre_history)
        return df_pivot

    def _pivot_by_account(self, df_history: pl.DataFrame) -> pl.DataFrame:
        """Pivota ``VL_CONTA`` por ``CONTA_NOME`` com cast Categorical para economia de RAM."""
        df_filtered = df_history.filter(pl.col("CONTA_NOME").is_not_null())
        df_filtered = df_filtered.with_columns(
            pl.col("CNPJ_CIA").cast(pl.Categorical),
            pl.col("CATEGORIA").cast(pl.Categorical),
            pl.col("SETOR_ATIV").cast(pl.Categorical),
            pl.col("CONTA_NOME").cast(pl.Categorical),
        )
        return df_filtered.pivot(
            values="VL_CONTA",
            index=["CNPJ_CIA", "ANO", "CATEGORIA", "SETOR_ATIV"],
            on="CONTA_NOME",
            aggregate_function="sum",
        )

    def _ensure_all_account_columns(self, df_pivot: pl.DataFrame) -> pl.DataFrame:
        """Garante presença de todas as colunas de conta esperadas (single evaluation)."""
        expected: set[str] = set()
        for setor_contas in self.config.contas.values():
            expected.update(setor_contas.values())
        expected.update(
            ["DEPREC_AMORT", "CAPEX_VAL", "DIVIDENDOS_PAGOS", "APLICACOES_CP", "APLICACOES_LP"]
        )

        missing_cols = [c for c in expected if c not in df_pivot.columns]
        if missing_cols:
            df_pivot = df_pivot.with_columns(
                [pl.lit(0.0).alias(c) for c in missing_cols]
            )
        return df_pivot.fill_null(value=0.0)

    @staticmethod
    def _consolidate_insurance_cash(df_pivot: pl.DataFrame) -> pl.DataFrame:
        """Seguradoras: Caixa = Caixa + Aplicações CP + Aplicações LP."""
        return df_pivot.with_columns(
            pl.when(pl.col("CATEGORIA") == Categoria.SEGURADORA.value)
            .then(
                pl.col("CAIXA_EQUIVALENTES")
                + pl.col("APLICACOES_CP")
                + pl.col("APLICACOES_LP")
            )
            .otherwise(pl.col("CAIXA_EQUIVALENTES"))
            .alias("CAIXA_EQUIVALENTES")
        )

    @staticmethod
    def _resolve_net_income(df_pivot: pl.DataFrame) -> pl.DataFrame:
        """Define LUCRO_FINAL via prioridade de 4 níveis.

        Prioridade:

            1. ``LUCRO_CONTROLADORA`` (conta 3.11.01)
            2. ``LUCRO_CONTROLADORA_BCO`` (conta 3.09.01) — fallback bancos
            3. ``LUCRO_LIQUIDO`` (conta 3.11) — consolidado total
            4. ``LUCRO_LIQUIDO_BCO`` (conta 3.09) — fallback final

        Usar o lucro da controladora (exclui participação de não controladores)
        alinha o LPA com o que analistas e data providers reportam.
        """
        return df_pivot.with_columns(
            pl.when(pl.col("LUCRO_CONTROLADORA") != 0)
            .then(pl.col("LUCRO_CONTROLADORA"))
            .when(pl.col("LUCRO_CONTROLADORA_BCO") != 0)
            .then(pl.col("LUCRO_CONTROLADORA_BCO"))
            .when(pl.col("LUCRO_LIQUIDO") != 0)
            .then(pl.col("LUCRO_LIQUIDO"))
            .otherwise(pl.col("LUCRO_LIQUIDO_BCO"))
            .alias("LUCRO_FINAL")
        )

    @staticmethod
    def _merge_shares_history(
        df_pivot: pl.DataFrame, df_fre_history: pl.DataFrame | None
    ) -> pl.DataFrame:
        """Junta histórico FRE (quantidade de ações) ou cria colunas vazias."""
        df_pivot = df_pivot.with_columns(
            pl.col("ANO").cast(pl.Int32),
            pl.col("CNPJ_CIA").cast(pl.Utf8),
        )

        if df_fre_history is not None and not df_fre_history.is_empty():
            df_fre_history = df_fre_history.with_columns(pl.col("ANO").cast(pl.Int32))
            df_pivot = df_pivot.join(df_fre_history, on=["CNPJ_CIA", "ANO"], how="left")
        else:
            df_pivot = df_pivot.with_columns(
                [
                    pl.lit(None, dtype=pl.Float64).alias("QTDE_ACOES"),
                    pl.lit(None, dtype=pl.Float64).alias("QTDE_ON"),
                    pl.lit(None, dtype=pl.Float64).alias("QTDE_PN"),
                ]
            )

        # Fallback: garantir existência de QTDE_ON e QTDE_PN mesmo se o FRE não tiver
        for col in ("QTDE_ON", "QTDE_PN"):
            if col not in df_pivot.columns:
                df_pivot = df_pivot.with_columns(
                    pl.lit(None, dtype=pl.Float64).alias(col)
                )
        return df_pivot

    @staticmethod
    def _merge_tickers(df_pivot: pl.DataFrame, df_matches: pl.DataFrame) -> pl.DataFrame:
        """Passo 4: Merge com mapa Ticker-CNPJ + rename + seleção final."""
        df_final = df_matches.join(df_pivot, on="CNPJ_CIA", how="inner")
        df_final = df_final.rename(
            {"RESULTADO_BRUTO": "LUCRO_BRUTO", "DIVIDA_TOTAL": "DIVIDA_BRUTA"}
        )
        if "LUCRO_LIQUIDO" in df_final.columns:
            df_final = df_final.drop("LUCRO_LIQUIDO")

        cols_fin = [c for c in _COLS_FIN if c in df_final.columns]
        df_wide = df_final.select(_COLS_ID + cols_fin)
        return df_wide.with_columns(pl.col("ANO").cast(pl.Int32)).sort(
            ["CATEGORIA", "TICKER", "ANO"]
        )

    @staticmethod
    def _apply_sector_assepsia(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 5: Anula métricas industriais para bancos e seguradoras.

        - Bancos: matéria-prima é dinheiro, métricas industriais não se aplicam.
        - Seguradoras: aplicações financeiras massivas distorcem ratios de dívida.
        """
        if "CATEGORIA" not in df_wide.columns:
            return df_wide

        df_wide = _nullify_for(df_wide, Categoria.FINANCEIRO, _METRICAS_ANULAR_BANCOS)
        df_wide = _nullify_for(df_wide, Categoria.SEGURADORA, _METRICAS_ANULAR_SEGURADORAS)
        return df_wide

    @staticmethod
    def _prepare_shifts(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 6: Gera colunas auxiliares (_prev, _BASE, rolling) num único passo.

        Consolidar todos os shifts num único ``with_columns`` permite ao
        Polars otimizar o plano de execução e reutilizar a estrutura
        ``window.over(TICKER)``.
        """
        shift_cols = list(dict.fromkeys(_FSCORE_SHIFT_COLS + _BENEISH_SHIFT_COLS))

        fscore_beneish_shifts = [
            pl.col(c).shift(1).over("TICKER").alias(f"{c}_prev")
            for c in shift_cols
            if c in df_wide.columns
        ]

        growth_shifts = [
            pl.col("RECEITA_LIQUIDA").shift(1).over("TICKER").alias("REC_PREV"),
            pl.col("LUCRO_FINAL").shift(1).over("TICKER").alias("LUCRO_PREV"),
            pl.col("RECEITA_LIQUIDA").shift(3).over("TICKER").alias("REC_3A_BASE"),
            pl.col("RECEITA_LIQUIDA").shift(5).over("TICKER").alias("REC_5A_BASE"),
            pl.col("LUCRO_FINAL").shift(3).over("TICKER").alias("LUCRO_3A_BASE"),
            pl.col("LUCRO_FINAL").shift(5).over("TICKER").alias("LUCRO_5A_BASE"),
        ]

        quant_shifts = [
            pl.col("ROE").shift(1).over("TICKER").alias("ROE_prev_q"),
            pl.col("MARGEM_LIQUIDA").shift(1).over("TICKER").alias("MARGEM_prev_q"),
            pl.col("ROE")
            .rolling_std(window_size=5, min_samples=5)
            .over("TICKER")
            .alias("ROE_std_5a"),
            pl.col("LUCRO_FINAL")
            .rolling_std(window_size=5, min_samples=5)
            .over("TICKER")
            .alias("LUCRO_std_5a"),
            pl.col("LUCRO_FINAL")
            .rolling_mean(window_size=5, min_samples=5)
            .over("TICKER")
            .alias("LUCRO_mean_5a"),
        ]

        return df_wide.with_columns(fscore_beneish_shifts + growth_shifts + quant_shifts)

    @staticmethod
    def _calculate_fscore(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 7: Piotroski F-Score (9 critérios, apenas INDUSTRIAL)."""
        criteria = [
            (pl.col("ROA") > 0),
            (pl.col("FCO") > 0),
            (pl.col("ROA") > pl.col("ROA_prev")),
            (pl.col("FCO") > pl.col("LUCRO_FINAL")),
            (pl.col("ALAVANCAGEM_LP") < pl.col("ALAVANCAGEM_LP_prev")),
            (pl.col("LIQUIDEZ_CORRENTE") > pl.col("LIQUIDEZ_CORRENTE_prev")),
            (pl.col("QTDE_ACOES") <= pl.col("QTDE_ACOES_prev")),
            (pl.col("MARGEM_BRUTA") > pl.col("MARGEM_BRUTA_prev")),
            (pl.col("GIRO_ATIVO") > pl.col("GIRO_ATIVO_prev")),
        ]
        points = sum(
            (criterion.cast(pl.Int32).fill_null(0) for criterion in criteria),
            start=pl.lit(0, dtype=pl.Int32),
        )
        return df_wide.with_columns(
            pl.when(pl.col("CATEGORIA") == Categoria.INDUSTRIAL.value)
            .then(
                pl.when(pl.col("ROA_prev").is_not_null()).then(points).otherwise(None)
            )
            .otherwise(None)
            .alias("F_SCORE")
        )

    @staticmethod
    def _calculate_beneish(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 8: Beneish M-Score (anti-fraude, apenas INDUSTRIAL).

        Fórmula original (Beneish, 1999)::

            M = -4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
                + 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI

        Onde cada termo é um índice anual (ano atual / ano anterior) de uma
        dimensão contábil específica. Threshold: ``M > -2.22`` indica risco.
        """
        m_score_formula = (
            -4.84
            + 0.920 * _beneish_dsri()
            + 0.528 * _beneish_gmi()
            + 0.404 * _beneish_aqi()
            + 0.892 * _beneish_sgi()
            + 0.115 * _beneish_depi()
            - 0.172 * _beneish_sgai()
            + 4.679 * _beneish_tata()
            - 0.327 * _beneish_lvgi()
        )

        has_history = pl.col("RECEITA_LIQUIDA_prev").is_not_null() & pl.col(
            "ATIVO_TOTAL_prev"
        ).is_not_null()

        return df_wide.with_columns(
            pl.when(pl.col("CATEGORIA") == Categoria.INDUSTRIAL.value)
            .then(pl.when(has_history).then(m_score_formula).otherwise(None))
            .otherwise(None)
            .alias("BENEISH_M")
        )

    @staticmethod
    def _calculate_growth_and_quant(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 9: Crescimento (YoY + CAGR) + 5 fatores quantitativos.

        Agrupa as expressões num único ``with_columns`` para reduzir a
        quantidade de passes sobre o DataFrame.
        """
        df_wide = df_wide.with_columns(
            _yoy_growth_expressions() + _cagr_expressions() + _quant_factor_expressions()
        )
        # Assepsia do Cash Conversion para bancos (FCO distorcido por captação)
        return _nullify_for(df_wide, Categoria.FINANCEIRO, ["CASH_CONVERSION"])

    @staticmethod
    def _calculate_efficiency_and_quality(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 10: 10 indicadores de eficiência operacional e qualidade.

        Indicadores (Camada 1) — todos usam colunas já presentes:

            1. MARGEM_FCO           — FCO / Receita
            2. MARGEM_FCL           — FCL / Receita
            3. CASH_ROA             — FCO / Ativo Total
            4. PMR                  — Contas a Receber / Receita × 365
            5. CAPITAL_DE_GIRO      — Ativo Circulante - Passivo Circulante
            6. ROCE                 — EBIT / (Ativo - Passivo Circulante)
            7. NOPAT                — EBIT × (1 - IR+CSLL Brasil)
            8. REINVESTMENT_RATE    — |CAPEX| / |Depreciação|
            9. SUSTAINABLE_GROWTH   — ROE × (1 - Payout)  [universal 3 setores]
            10. CASH_RATIO          — Caixa / Passivo Circulante

        Assepsia: 9 dos 10 anulados para bancos (SUSTAINABLE_GROWTH é universal).
        """
        df_wide = df_wide.with_columns(_efficiency_expressions())
        return _nullify_for(df_wide, Categoria.FINANCEIRO, _EFICIENCIA_ANULAR_BANCOS)

    @staticmethod
    def _round_and_finalize(df_wide: pl.DataFrame) -> pl.DataFrame:
        """Passo 11: Cleanup de colunas temporárias + arredondamentos + rename."""
        shift_cols = list(dict.fromkeys(_FSCORE_SHIFT_COLS + _BENEISH_SHIFT_COLS))
        prev_cols = [f"{c}_prev" for c in shift_cols if f"{c}_prev" in df_wide.columns]
        aux_cols = [
            "ALAVANCAGEM_LP", "REC_PREV", "LUCRO_PREV",
            "REC_3A_BASE", "REC_5A_BASE", "LUCRO_3A_BASE", "LUCRO_5A_BASE",
            "ROE_prev_q", "MARGEM_prev_q", "ROE_std_5a", "LUCRO_std_5a", "LUCRO_mean_5a",
        ]
        temp_cols = prev_cols + aux_cols
        df_wide = df_wide.drop([c for c in temp_cols if c in df_wide.columns])

        df_wide = df_wide.with_columns(
            [pl.col(c).round(4) for c in _COLS_ROUND_4D if c in df_wide.columns]
            + [pl.col(c).round(2) for c in _COLS_ROUND_2D if c in df_wide.columns]
        )

        # Rename final: LUCRO_FINAL é convenção interna; LUCRO_LIQUIDO é público.
        return df_wide.rename({"LUCRO_FINAL": "LUCRO_LIQUIDO"})


# --- Fórmulas do Beneish M-Score (1 termo por função) ---


def _beneish_dsri() -> pl.Expr:
    """DSRI — Days Sales in Receivables Index (índice de contas a receber)."""
    return safe_div(
        safe_div("CONTAS_A_RECEBER", "RECEITA_LIQUIDA"),
        safe_div("CONTAS_A_RECEBER_prev", "RECEITA_LIQUIDA_prev"),
    )


def _beneish_gmi() -> pl.Expr:
    """GMI — Gross Margin Index (deterioração da margem bruta)."""
    return safe_div("MARGEM_BRUTA_prev", "MARGEM_BRUTA")


def _beneish_aqi() -> pl.Expr:
    """AQI — Asset Quality Index (qualidade dos ativos não-correntes)."""
    non_operating_current = pl.col("ATIVO_CIRCULANTE") + pl.col("IMOBILIZADO").fill_null(0)
    non_operating_prev = pl.col("ATIVO_CIRCULANTE_prev") + pl.col("IMOBILIZADO_prev").fill_null(0)
    return safe_div(
        1 - safe_div(non_operating_current, pl.col("ATIVO_TOTAL")),
        1 - safe_div(non_operating_prev, pl.col("ATIVO_TOTAL_prev")),
    )


def _beneish_sgi() -> pl.Expr:
    """SGI — Sales Growth Index (crescimento de receita)."""
    return safe_div("RECEITA_LIQUIDA", "RECEITA_LIQUIDA_prev")


def _beneish_depi() -> pl.Expr:
    """DEPI — Depreciation Index (aceleração de depreciação)."""
    dep_current = pl.col("DEPREC_AMORT").abs()
    dep_prev = pl.col("DEPREC_AMORT_prev").abs()
    imob_current = pl.col("IMOBILIZADO").fill_null(0)
    imob_prev = pl.col("IMOBILIZADO_prev").fill_null(0)
    return safe_div(
        safe_div(dep_prev, dep_prev + imob_prev),
        safe_div(dep_current, dep_current + imob_current),
    )


def _beneish_sgai() -> pl.Expr:
    """SGAI — Sales General & Admin Expenses Index."""
    return safe_div(
        safe_div(pl.col("DESPESAS_OPERACIONAIS").fill_null(0).abs(), pl.col("RECEITA_LIQUIDA")),
        safe_div(pl.col("DESPESAS_OPERACIONAIS_prev").fill_null(0).abs(), pl.col("RECEITA_LIQUIDA_prev")),
    )


def _beneish_tata() -> pl.Expr:
    """TATA — Total Accruals to Total Assets (accruals sobre ativos)."""
    return safe_div("ACCRUALS", "ATIVO_TOTAL")


def _beneish_lvgi() -> pl.Expr:
    """LVGI — Leverage Index (variação da alavancagem)."""
    return safe_div(
        safe_div("DIVIDA_BRUTA", "ATIVO_TOTAL"),
        safe_div("DIVIDA_BRUTA_prev", "ATIVO_TOTAL_prev"),
    )


# --- Expressões de crescimento e fatores quantitativos ---


def _yoy_growth_expressions() -> list[pl.Expr]:
    """Crescimento Year-over-Year (protegido contra bases negativas/zero)."""
    return [
        pl.when(pl.col("REC_PREV") > 0)
        .then((pl.col("RECEITA_LIQUIDA") / pl.col("REC_PREV")) - 1)
        .otherwise(None)
        .alias("CRESC_RECEITA_YOY"),
        pl.when(pl.col("LUCRO_PREV") > 0)
        .then((pl.col("LUCRO_FINAL") / pl.col("LUCRO_PREV")) - 1)
        .otherwise(None)
        .alias("CRESC_LUCRO_YOY"),
    ]


def _cagr_expressions() -> list[pl.Expr]:
    """CAGR 3a e 5a para Receita e Lucro (agnóstico ao setor)."""
    return [
        _cagr_expr("RECEITA_LIQUIDA", "REC_3A_BASE", years=3).alias("CAGR_RECEITA_3A"),
        _cagr_expr("RECEITA_LIQUIDA", "REC_5A_BASE", years=5).alias("CAGR_RECEITA_5A"),
        _cagr_expr("LUCRO_FINAL", "LUCRO_3A_BASE", years=3).alias("CAGR_LUCRO_3A"),
        _cagr_expr("LUCRO_FINAL", "LUCRO_5A_BASE", years=5).alias("CAGR_LUCRO_5A"),
    ]


def _cagr_expr(current_col: str, base_col: str, years: int) -> pl.Expr:
    """Taxa composta de crescimento anual: ``(current / base)^(1/years) - 1``.

    Retorna ``None`` se a base ou o valor atual for não-positivo (evita
    raízes complexas e valores enganosos).
    """
    base = pl.col(base_col)
    current = pl.col(current_col)
    valid = (base > 0) & (current > 0)
    return (
        pl.when(valid)
        .then((current / base).pow(1.0 / years) - 1)
        .otherwise(None)
    )


def _quant_factor_expressions() -> list[pl.Expr]:
    """Fatores quantitativos: Cash Conversion, Earnings Stability, ΔROE, ΔMargem."""
    return [
        pl.when(pl.col("LUCRO_FINAL") > 0)
        .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
        .otherwise(None)
        .alias("CASH_CONVERSION"),
        pl.col("ROE_std_5a").alias("EARNINGS_STABILITY"),
        pl.when(pl.col("LUCRO_mean_5a") > 0)
        .then(pl.col("LUCRO_std_5a") / pl.col("LUCRO_mean_5a"))
        .otherwise(None)
        .alias("VOL_LUCRO"),
        pl.when(pl.col("ROE_prev_q").is_not_null())
        .then(pl.col("ROE") - pl.col("ROE_prev_q"))
        .otherwise(None)
        .alias("DELTA_ROE"),
        pl.when(pl.col("MARGEM_prev_q").is_not_null())
        .then(pl.col("MARGEM_LIQUIDA") - pl.col("MARGEM_prev_q"))
        .otherwise(None)
        .alias("DELTA_MARGEM"),
    ]


# --- Expressões de eficiência e qualidade ---


def _efficiency_expressions() -> list[pl.Expr]:
    """10 indicadores de eficiência operacional e qualidade."""
    return [
        _margin_of_cash_flow("FCO").alias("MARGEM_FCO"),
        _margin_of_cash_flow("FCL").alias("MARGEM_FCL"),
        _cash_roa().alias("CASH_ROA"),
        _receivables_days().alias("PMR"),
        (pl.col("ATIVO_CIRCULANTE") - pl.col("PASSIVO_CIRCULANTE")).alias("CAPITAL_DE_GIRO"),
        _roce().alias("ROCE"),
        (pl.col("EBIT") * BRAZIL_AFTER_TAX).alias("NOPAT"),
        _reinvestment_rate().alias("REINVESTMENT_RATE"),
        _sustainable_growth().alias("SUSTAINABLE_GROWTH"),
        _cash_ratio().alias("CASH_RATIO"),
    ]


def _margin_of_cash_flow(flow_col: str) -> pl.Expr:
    """Margem de fluxo de caixa = ``flow_col / Receita Líquida``."""
    return (
        pl.when(pl.col("RECEITA_LIQUIDA") > 0)
        .then(pl.col(flow_col) / pl.col("RECEITA_LIQUIDA"))
        .otherwise(None)
    )


def _cash_roa() -> pl.Expr:
    """Cash ROA = FCO / Ativo Total (ROA em caixa real, não lucro contábil)."""
    return (
        pl.when(pl.col("ATIVO_TOTAL") > 0)
        .then(pl.col("FCO") / pl.col("ATIVO_TOTAL"))
        .otherwise(None)
    )


def _receivables_days() -> pl.Expr:
    """PMR = (Contas a Receber / Receita) × 365."""
    return (
        pl.when(pl.col("RECEITA_LIQUIDA") > 0)
        .then((pl.col("CONTAS_A_RECEBER") / pl.col("RECEITA_LIQUIDA")) * _DIAS_NO_ANO)
        .otherwise(None)
    )


def _roce() -> pl.Expr:
    """ROCE = EBIT / (Ativo Total - Passivo Circulante)."""
    capital_employed = pl.col("ATIVO_TOTAL") - pl.col("PASSIVO_CIRCULANTE")
    return (
        pl.when(capital_employed > 0)
        .then(pl.col("EBIT") / capital_employed)
        .otherwise(None)
    )


def _reinvestment_rate() -> pl.Expr:
    """Reinvestment Rate = |CAPEX| / |Depreciação|.

    > 1: empresa expandindo capacidade. < 1: consumindo ativos sem repor.
    """
    return (
        pl.when(pl.col("DEPREC_AMORT").abs() > 0)
        .then(pl.col("CAPEX").abs() / pl.col("DEPREC_AMORT").abs())
        .otherwise(None)
    )


def _sustainable_growth() -> pl.Expr:
    """Sustainable Growth Rate = ROE × (1 - Payout).

    Indicador universal (aplica aos 3 setores) pois depende apenas de
    ROE e Payout — métricas imunes à distorção setorial.
    Payout é clipado em [0, 1] para evitar casos patológicos.
    """
    payout_clipped = pl.col("PAYOUT").clip(lower_bound=0, upper_bound=1)
    return (
        pl.when(pl.col("ROE").is_not_null() & pl.col("PAYOUT").is_not_null())
        .then(pl.col("ROE") * (1 - payout_clipped))
        .otherwise(None)
    )


def _cash_ratio() -> pl.Expr:
    """Cash Ratio = Caixa / Passivo Circulante (liquidez imediata)."""
    return (
        pl.when(pl.col("PASSIVO_CIRCULANTE") > 0)
        .then(pl.col("CAIXA_EQUIVALENTES") / pl.col("PASSIVO_CIRCULANTE"))
        .otherwise(None)
    )
