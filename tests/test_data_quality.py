"""Testes unitários do módulo synetra.data_quality.

Cobre:
    - models: Severity, FlagType, QualityIssue, TickerQuality, QualityReport
    - checks: cada verificação isolada
    - report: construção do relatório e exportação CSV

Princípios F.I.R.S.T.: fixtures pequenas, sem I/O de rede.
"""
from __future__ import annotations

import pathlib

import polars as pl
import pytest

from synetra.data_quality.checks import (
    _MAX_YAHOO_STALE_YEARS,
    _MIN_YEARS_FOR_ESTABLISHED_TICKER,
    _YEARS_WITHOUT_CVM_TO_SUSPECT_DELISTING,
    check_likely_delisted,
    check_no_yahoo_history,
    check_recent_listing,
    check_temporal_gaps,
    check_ticker_may_be_wrong,
    check_yahoo_stale,
    run_all_checks,
)
from synetra.data_quality.models import (
    FlagType,
    QualityIssue,
    QualityReport,
    Severity,
    TickerQuality,
)
from synetra.data_quality.report import (
    _find_gap_years,
    build_quality_report,
    export_quality_report,
)

# --- Helper de fixture ---


def _make_ticker(
    ticker: str = "PETR4",
    *,
    categoria: str = "INDUSTRIAL",
    ultimo_ano_cvm: int = 2024,
    ultimo_ano_yahoo: int | None = 2024,
    anos_cvm_total: int = 15,
    anos_com_preco_yahoo: int = 15,
    tem_preco_atual: bool = True,
) -> TickerQuality:
    """Cria um TickerQuality com defaults razoáveis para casos saudáveis."""
    return TickerQuality(
        ticker=ticker,
        categoria=categoria,
        razao_cvm=f"{ticker} S.A.",
        tem_preco_atual=tem_preco_atual,
        ultimo_ano_cvm=ultimo_ano_cvm,
        ultimo_ano_yahoo=ultimo_ano_yahoo,
        anos_cvm_total=anos_cvm_total,
        anos_com_preco_yahoo=anos_com_preco_yahoo,
    )


# --- models — Severity e Enums ---


class TestSeverityEnum:
    """Severity tem ordenação implícita para priorização."""

    def test_all_values_are_strings(self) -> None:
        assert Severity.OK == "OK"
        assert Severity.LOW == "LOW"
        assert Severity.MEDIUM == "MEDIUM"
        assert Severity.HIGH == "HIGH"


class TestFlagTypeEnum:
    def test_all_flags_have_distinct_values(self) -> None:
        values = [f.value for f in FlagType]
        assert len(values) == len(set(values))


# --- models — TickerQuality ---


class TestTickerQualityProperties:
    """Propriedades derivadas (severity, flags, gap_anos_preco)."""

    def test_severity_ok_when_no_issues(self) -> None:
        ticker = _make_ticker()
        assert ticker.severity == Severity.OK

    def test_severity_is_max_of_issues(self) -> None:
        ticker = _make_ticker()
        ticker.issues = [
            QualityIssue(FlagType.TEMPORAL_GAP, Severity.LOW),
            QualityIssue(FlagType.YAHOO_STALE, Severity.MEDIUM),
            QualityIssue(FlagType.LIKELY_DELISTED, Severity.HIGH),
        ]
        assert ticker.severity == Severity.HIGH

    def test_flags_dedupes_preserving_order(self) -> None:
        ticker = _make_ticker()
        ticker.issues = [
            QualityIssue(FlagType.YAHOO_STALE, Severity.MEDIUM, "a"),
            QualityIssue(FlagType.TEMPORAL_GAP, Severity.LOW, "b"),
            QualityIssue(FlagType.YAHOO_STALE, Severity.MEDIUM, "c"),
        ]
        assert ticker.flags == [FlagType.YAHOO_STALE, FlagType.TEMPORAL_GAP]

    def test_gap_anos_preco_none_quando_sem_yahoo(self) -> None:
        ticker = _make_ticker(ultimo_ano_yahoo=None)
        assert ticker.gap_anos_preco is None

    def test_gap_anos_preco_calcula_diferenca(self) -> None:
        ticker = _make_ticker(ultimo_ano_cvm=2024, ultimo_ano_yahoo=2022)
        assert ticker.gap_anos_preco == 2


# --- models — QualityReport ---


class TestQualityReportFromTickers:
    def test_counters_agregados(self) -> None:
        ok = _make_ticker("OK")
        low = _make_ticker("LOW")
        low.issues = [QualityIssue(FlagType.TEMPORAL_GAP, Severity.LOW)]
        high = _make_ticker("HIGH")
        high.issues = [QualityIssue(FlagType.NO_YAHOO_HISTORY, Severity.HIGH)]

        report = QualityReport.from_tickers([ok, low, high])
        assert report.total == 3
        assert report.ok_count == 1
        assert report.low_count == 1
        assert report.medium_count == 0
        assert report.high_count == 1


# --- checks — individuais ---


class TestCheckNoYahooHistory:
    def test_sem_yahoo_sinaliza_high(self) -> None:
        ticker = _make_ticker(anos_com_preco_yahoo=0, ultimo_ano_yahoo=None)
        issues = check_no_yahoo_history(ticker)
        assert len(issues) == 1
        assert issues[0].flag == FlagType.NO_YAHOO_HISTORY
        assert issues[0].severity == Severity.HIGH

    def test_com_yahoo_nao_sinaliza(self) -> None:
        ticker = _make_ticker()
        assert check_no_yahoo_history(ticker) == []


class TestCheckYahooStale:
    def test_gap_dentro_do_limite_nao_sinaliza(self) -> None:
        ticker = _make_ticker(
            ultimo_ano_cvm=2024,
            ultimo_ano_yahoo=2024 - _MAX_YAHOO_STALE_YEARS,
        )
        assert check_yahoo_stale(ticker, current_year=2025) == []

    def test_gap_maior_que_limite_sinaliza_medium(self) -> None:
        ticker = _make_ticker(
            ultimo_ano_cvm=2025,
            ultimo_ano_yahoo=2023,  # gap de 2 anos, limite é 1
        )
        issues = check_yahoo_stale(ticker, current_year=2025)
        assert len(issues) == 1
        assert issues[0].flag == FlagType.YAHOO_STALE
        assert issues[0].severity == Severity.MEDIUM

    def test_sem_yahoo_nao_sinaliza(self) -> None:
        """Caso sem Yahoo já é tratado em check_no_yahoo_history."""
        ticker = _make_ticker(ultimo_ano_yahoo=None)
        assert check_yahoo_stale(ticker, current_year=2025) == []


class TestCheckLikelyDelisted:
    def test_dado_recente_nao_sinaliza(self) -> None:
        ticker = _make_ticker(ultimo_ano_cvm=2025)
        assert check_likely_delisted(ticker, current_year=2025) == []

    def test_dado_antigo_sinaliza_high(self) -> None:
        ticker = _make_ticker(
            ultimo_ano_cvm=2025 - _YEARS_WITHOUT_CVM_TO_SUSPECT_DELISTING - 1,
        )
        issues = check_likely_delisted(ticker, current_year=2025)
        assert len(issues) == 1
        assert issues[0].severity == Severity.HIGH


class TestCheckTickerMayBeWrong:
    def test_pn_sem_yahoo_sinaliza_medium(self) -> None:
        # CYRE4: PN (final 4) sem histórico → provavelmente deveria ser CYRE3
        ticker = _make_ticker(
            ticker="CYRE4",
            anos_com_preco_yahoo=0,
            ultimo_ano_yahoo=None,
        )
        issues = check_ticker_may_be_wrong(ticker)
        assert len(issues) == 1
        assert issues[0].flag == FlagType.TICKER_MAY_BE_WRONG
        assert issues[0].severity == Severity.MEDIUM

    def test_unit_sem_yahoo_sinaliza(self) -> None:
        ticker = _make_ticker(
            ticker="SAPR11",
            anos_com_preco_yahoo=0,
            ultimo_ano_yahoo=None,
        )
        issues = check_ticker_may_be_wrong(ticker)
        assert len(issues) == 1

    def test_on_sem_yahoo_nao_sinaliza_ticker_errado(self) -> None:
        """ON sem Yahoo é NO_YAHOO_HISTORY, não TICKER_WRONG (é a classe dominante)."""
        ticker = _make_ticker(
            ticker="ARND3",
            anos_com_preco_yahoo=0,
            ultimo_ano_yahoo=None,
        )
        assert check_ticker_may_be_wrong(ticker) == []

    def test_com_yahoo_nao_sinaliza(self) -> None:
        ticker = _make_ticker(ticker="PETR4")
        assert check_ticker_may_be_wrong(ticker) == []


class TestCheckTemporalGaps:
    def test_sem_gaps_nao_sinaliza(self) -> None:
        ticker = _make_ticker()
        assert check_temporal_gaps(ticker, gap_years=[]) == []

    def test_com_gaps_sinaliza_low(self) -> None:
        ticker = _make_ticker()
        issues = check_temporal_gaps(ticker, gap_years=[2020, 2021])
        assert len(issues) == 1
        assert issues[0].flag == FlagType.TEMPORAL_GAP
        assert issues[0].severity == Severity.LOW
        assert "2020" in issues[0].detail


class TestCheckRecentListing:
    def test_empresa_estabelecida_nao_sinaliza(self) -> None:
        ticker = _make_ticker(anos_cvm_total=_MIN_YEARS_FOR_ESTABLISHED_TICKER)
        assert check_recent_listing(ticker) == []

    def test_empresa_recente_sinaliza_low(self) -> None:
        ticker = _make_ticker(
            anos_cvm_total=_MIN_YEARS_FOR_ESTABLISHED_TICKER - 1
        )
        issues = check_recent_listing(ticker)
        assert len(issues) == 1
        assert issues[0].severity == Severity.LOW


# --- run_all_checks — integração ---


class TestRunAllChecks:
    def test_ticker_saudavel_sem_issues(self) -> None:
        ticker = _make_ticker()
        issues = run_all_checks(ticker, current_year=2025, gap_years=[])
        assert issues == []

    def test_ticker_com_multiplos_problemas(self) -> None:
        """CYRE4 caso real: PN sem Yahoo + temporal gaps."""
        ticker = _make_ticker(
            ticker="CYRE4",
            anos_com_preco_yahoo=0,
            ultimo_ano_yahoo=None,
        )
        issues = run_all_checks(
            ticker, current_year=2025, gap_years=[2018]
        )
        flags = {i.flag for i in issues}
        assert FlagType.NO_YAHOO_HISTORY in flags
        assert FlagType.TICKER_MAY_BE_WRONG in flags
        assert FlagType.TEMPORAL_GAP in flags


# --- report — helpers ---


class TestFindGapYears:
    def test_sem_gaps_retorna_vazio(self) -> None:
        assert _find_gap_years([2020, 2021, 2022]) == []

    def test_com_gap_simples(self) -> None:
        assert _find_gap_years([2020, 2022]) == [2021]

    def test_multiplos_gaps(self) -> None:
        assert _find_gap_years([2020, 2023, 2025]) == [2021, 2022, 2024]

    def test_lista_muito_pequena(self) -> None:
        assert _find_gap_years([2020]) == []
        assert _find_gap_years([]) == []


# --- build_quality_report — integração ---


class TestBuildQualityReport:
    """Testes de integração do pipeline completo de data quality."""

    @pytest.fixture
    def df_history_petr4(self) -> pl.DataFrame:
        """Histórico saudável de PETR4 (16 anos com preço)."""
        rows = [
            {
                "TICKER": "PETR4",
                "CATEGORIA": "INDUSTRIAL",
                "ANO": ano,
                "PRECO_FIM_ANO": 30.0 + ano - 2010,
            }
            for ano in range(2010, 2026)
        ]
        return pl.DataFrame(rows)

    @pytest.fixture
    def df_snapshot_petr4(self) -> pl.DataFrame:
        return pl.DataFrame(
            [
                {
                    "TICKER": "PETR4",
                    "CATEGORIA": "INDUSTRIAL",
                    "RAZAO_CVM": "PETROLEO BRASILEIRO S.A.",
                    "PRECO_ATUAL": 45.67,
                }
            ]
        )

    def test_ticker_saudavel_sem_issues(
        self,
        df_history_petr4: pl.DataFrame,
        df_snapshot_petr4: pl.DataFrame,
    ) -> None:
        report = build_quality_report(
            df_history_petr4, df_snapshot_petr4, current_year=2025
        )
        assert report.total == 1
        assert report.ok_count == 1
        assert report.high_count == 0
        assert report.tickers[0].severity == Severity.OK

    def test_ticker_sem_yahoo_vira_high(self) -> None:
        """Ticker CVM sem nenhum preço Yahoo → HIGH."""
        history = pl.DataFrame(
            [
                {"TICKER": "ARND3", "CATEGORIA": "INDUSTRIAL",
                 "ANO": 2024, "PRECO_FIM_ANO": None},
            ]
        )
        snapshot = pl.DataFrame(
            [
                {
                    "TICKER": "ARND3",
                    "CATEGORIA": "INDUSTRIAL",
                    "RAZAO_CVM": "ARANDU",
                    "PRECO_ATUAL": None,
                }
            ]
        )
        report = build_quality_report(history, snapshot, current_year=2025)
        assert report.high_count == 1
        ticker = report.tickers[0]
        assert FlagType.NO_YAHOO_HISTORY in ticker.flags


class TestExportQualityReport:
    def test_exporta_csv_com_separador_ponto_virgula(
        self, tmp_path: pathlib.Path
    ) -> None:
        ticker = _make_ticker()
        ticker.issues = [
            QualityIssue(FlagType.TEMPORAL_GAP, Severity.LOW, "detail 1")
        ]
        report = QualityReport.from_tickers([ticker])

        output = tmp_path / "dq.csv"
        export_quality_report(report, output)

        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert ";" in content  # separator
        assert "TICKER" in content  # header
        assert "PETR4" in content  # dado

    def test_csv_ordena_por_severidade_desc(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Tickers HIGH aparecem no topo (facilita inspeção manual)."""
        ok = _make_ticker("OK_TICKER")
        high = _make_ticker("HIGH_TICKER")
        high.issues = [
            QualityIssue(FlagType.NO_YAHOO_HISTORY, Severity.HIGH, "d")
        ]
        report = QualityReport.from_tickers([ok, high])

        output = tmp_path / "dq.csv"
        export_quality_report(report, output)

        lines = output.read_text(encoding="utf-8").strip().split("\n")
        # Pular header, primeira linha de dado deve ser o HIGH
        first_data_row = lines[1]
        assert "HIGH_TICKER" in first_data_row
