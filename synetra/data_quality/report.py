"""Orquestra geração, exportação e log do relatório de Data Quality.

Fluxo principal (``run_data_quality_audit``):

    1. Para cada ticker no snapshot, coleta metadados do histórico.
    2. Roda todos os checks de :mod:`synetra.data_quality.checks`.
    3. Consolida em um :class:`QualityReport`.
    4. Exporta CSV (``data_quality_report.csv``).
    5. Loga resumo executivo.
"""
from __future__ import annotations

import pathlib
from datetime import date

import polars as pl
from loguru import logger

from synetra.data_quality.checks import run_all_checks
from synetra.data_quality.models import (
    QualityReport,
    Severity,
    TickerQuality,
)

# --- Constantes ---

#: Nome padrão do arquivo CSV de saída.
DEFAULT_REPORT_FILENAME: str = "data_quality_report.csv"

#: Separador padrão (segue convenção do projeto).
_CSV_SEPARATOR: str = ";"

#: Largura do separador visual no log summary.
_LOG_SEPARATOR_WIDTH: int = 60


# --- Funções públicas ---


def run_data_quality_audit(
    df_history: pl.DataFrame,
    df_snapshot: pl.DataFrame,
    output_path: str | pathlib.Path = DEFAULT_REPORT_FILENAME,
    current_year: int | None = None,
) -> QualityReport:
    """Roda o pipeline completo de data quality.

    Args:
        df_history: Série histórica financeira (output do pipeline).
        df_snapshot: Snapshot atual (output do pipeline).
        output_path: Caminho do CSV de saída.
        current_year: Ano de referência para cálculos de "stale/delisted".
            Se ``None``, usa o ano atual do sistema.

    Returns:
        ``QualityReport`` consolidado (também é escrito em CSV e logado).
    """
    year = current_year if current_year is not None else date.today().year

    report = build_quality_report(df_history, df_snapshot, current_year=year)
    export_quality_report(report, output_path)
    log_quality_summary(report)
    return report


def build_quality_report(
    df_history: pl.DataFrame,
    df_snapshot: pl.DataFrame,
    current_year: int,
) -> QualityReport:
    """Constrói o relatório sem escrever nem logar (útil pra testes)."""
    tickers_qa = _audit_all_tickers(df_history, df_snapshot, current_year)
    return QualityReport.from_tickers(tickers_qa)


def export_quality_report(
    report: QualityReport, output_path: str | pathlib.Path
) -> None:
    """Escreve o relatório em CSV."""
    df = _report_to_dataframe(report)
    df.write_csv(pathlib.Path(output_path), separator=_CSV_SEPARATOR)
    logger.info(
        "Data Quality Report: {} ({} tickers)", output_path, report.total
    )


def log_quality_summary(report: QualityReport) -> None:
    """Emite resumo executivo do relatório no log."""
    separator = "=" * _LOG_SEPARATOR_WIDTH
    logger.info(separator)
    logger.info("DATA QUALITY AUDIT")
    logger.info(separator)

    _log_severity_breakdown(report)
    _log_flag_breakdown(report)
    _log_high_severity_tickers(report)

    logger.info(separator)


# --- Helpers internos — auditoria por ticker ---


def _audit_all_tickers(
    df_history: pl.DataFrame,
    df_snapshot: pl.DataFrame,
    current_year: int,
) -> list[TickerQuality]:
    """Itera cada ticker do snapshot e produz o resumo de qualidade."""
    if df_snapshot.is_empty():
        logger.warning("Snapshot vazio — data quality audit retornará vazio.")
        return []

    # Pré-agregação do histórico por ticker: pula trabalho repetido.
    history_stats = _precompute_history_stats(df_history)

    results: list[TickerQuality] = []
    for row in df_snapshot.iter_rows(named=True):
        ticker_code = row["TICKER"]
        stats = history_stats.get(ticker_code, _empty_history_stats())
        quality = _build_ticker_quality(
            snapshot_row=row, history_stats=stats
        )
        quality.issues = run_all_checks(
            quality, current_year=current_year, gap_years=stats["gap_years"]
        )
        results.append(quality)
    return results


def _precompute_history_stats(df_history: pl.DataFrame) -> dict[str, dict]:
    """Calcula metadados por ticker uma só vez (ao invés de N vezes).

    Para cada ticker, retorna:
        - ultimo_ano_cvm, anos_cvm_total
        - ultimo_ano_yahoo, anos_com_preco_yahoo
        - gap_years (anos ausentes na série)
    """
    if df_history.is_empty():
        return {}

    # Agrupa cada ticker — 1 linha por ticker com agregados
    has_price = pl.col("PRECO_FIM_ANO").is_not_null()
    agg = df_history.group_by("TICKER").agg(
        [
            pl.col("ANO").max().alias("ultimo_ano_cvm"),
            pl.col("ANO").min().alias("primeiro_ano_cvm"),
            pl.col("ANO").n_unique().alias("anos_cvm_total"),
            pl.col("ANO").filter(has_price).max().alias("ultimo_ano_yahoo"),
            pl.col("ANO").filter(has_price).count().alias("anos_com_preco_yahoo"),
        ]
    )

    # Para gaps, precisamos da lista de anos por ticker
    anos_por_ticker = (
        df_history.group_by("TICKER")
        .agg(pl.col("ANO").unique().sort().alias("anos"))
    )
    gaps_map = {
        row["TICKER"]: _find_gap_years(row["anos"])
        for row in anos_por_ticker.iter_rows(named=True)
    }

    stats: dict[str, dict] = {}
    for row in agg.iter_rows(named=True):
        ticker = row["TICKER"]
        stats[ticker] = {
            "ultimo_ano_cvm": int(row["ultimo_ano_cvm"]),
            "anos_cvm_total": int(row["anos_cvm_total"]),
            "ultimo_ano_yahoo": (
                int(row["ultimo_ano_yahoo"])
                if row["ultimo_ano_yahoo"] is not None
                else None
            ),
            "anos_com_preco_yahoo": int(row["anos_com_preco_yahoo"]),
            "gap_years": gaps_map.get(ticker, []),
        }
    return stats


def _empty_history_stats() -> dict:
    """Stats default para ticker sem histórico CVM."""
    return {
        "ultimo_ano_cvm": 0,
        "anos_cvm_total": 0,
        "ultimo_ano_yahoo": None,
        "anos_com_preco_yahoo": 0,
        "gap_years": [],
    }


def _find_gap_years(anos: list[int]) -> list[int]:
    """Retorna anos ausentes entre min e max da série."""
    if len(anos) < 2:
        return []
    anos_set = set(anos)
    expected = set(range(min(anos), max(anos) + 1))
    return sorted(expected - anos_set)


def _build_ticker_quality(
    snapshot_row: dict, history_stats: dict
) -> TickerQuality:
    """Cria TickerQuality combinando snapshot + stats históricos."""
    return TickerQuality(
        ticker=snapshot_row["TICKER"],
        categoria=snapshot_row.get("CATEGORIA", ""),
        razao_cvm=snapshot_row.get("RAZAO_CVM", ""),
        tem_preco_atual=snapshot_row.get("PRECO_ATUAL") is not None,
        ultimo_ano_cvm=history_stats["ultimo_ano_cvm"],
        ultimo_ano_yahoo=history_stats["ultimo_ano_yahoo"],
        anos_cvm_total=history_stats["anos_cvm_total"],
        anos_com_preco_yahoo=history_stats["anos_com_preco_yahoo"],
    )


# --- Helpers internos — serialização em DataFrame ---


def _report_to_dataframe(report: QualityReport) -> pl.DataFrame:
    """Converte o relatório em DataFrame Polars pronto para CSV."""
    rows = [_ticker_to_dict(t) for t in report.tickers]
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort(
        ["SEVERIDADE_ORDEM", "TICKER"], descending=[True, False]
    ).drop("SEVERIDADE_ORDEM")


def _ticker_to_dict(ticker: TickerQuality) -> dict:
    """Serializa um TickerQuality em dict pra CSV."""
    severity_order = {
        Severity.OK: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3,
    }
    return {
        "TICKER": ticker.ticker,
        "CATEGORIA": ticker.categoria,
        "RAZAO_CVM": ticker.razao_cvm,
        "SEVERIDADE": ticker.severity.value,
        "SEVERIDADE_ORDEM": severity_order[ticker.severity],
        "FLAGS": ",".join(f.value for f in ticker.flags),
        "TEM_PRECO_ATUAL": ticker.tem_preco_atual,
        "ULTIMO_ANO_CVM": ticker.ultimo_ano_cvm,
        "ULTIMO_ANO_YAHOO": ticker.ultimo_ano_yahoo,
        "GAP_ANOS_PRECO": ticker.gap_anos_preco,
        "ANOS_CVM_TOTAL": ticker.anos_cvm_total,
        "ANOS_COM_PRECO_YAHOO": ticker.anos_com_preco_yahoo,
        "DETALHES": " | ".join(i.detail for i in ticker.issues if i.detail),
    }


# --- Helpers internos — log summary ---


def _log_severity_breakdown(report: QualityReport) -> None:
    """Breakdown por severidade."""
    total = max(report.total, 1)
    logger.info("Total de tickers auditados: {}", report.total)
    logger.info(
        "  OK:      {:>4} ({:>5.1f}%)",
        report.ok_count, report.ok_count / total * 100,
    )
    logger.info(
        "  LOW:     {:>4} ({:>5.1f}%)",
        report.low_count, report.low_count / total * 100,
    )
    logger.info(
        "  MEDIUM:  {:>4} ({:>5.1f}%)",
        report.medium_count, report.medium_count / total * 100,
    )
    logger.info(
        "  HIGH:    {:>4} ({:>5.1f}%)",
        report.high_count, report.high_count / total * 100,
    )


def _log_flag_breakdown(report: QualityReport) -> None:
    """Contagem de ocorrências por tipo de flag."""
    flag_counts: dict[str, int] = {}
    for ticker in report.tickers:
        for flag in ticker.flags:
            flag_counts[flag.value] = flag_counts.get(flag.value, 0) + 1

    if not flag_counts:
        return

    logger.info("Ocorrências por flag:")
    for flag_name, count in sorted(flag_counts.items(), key=lambda x: -x[1]):
        logger.info("   {:<22s} {:>4}", flag_name, count)


def _log_high_severity_tickers(report: QualityReport) -> None:
    """Lista primeiros tickers com severidade HIGH (pra investigação manual)."""
    high = [t for t in report.tickers if t.severity == Severity.HIGH]
    if not high:
        return

    limit = 10
    logger.info(
        "Tickers HIGH (primeiros {}): {}",
        min(limit, len(high)),
        ", ".join(t.ticker for t in high[:limit]),
    )
