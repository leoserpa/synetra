"""Testes do Market Cap consolidado por empresa (ON + PN).

Valida que empresas com duas classes de ações (ex: Petrobras, Itaú, Bradesco)
produzem o MESMO Market Cap consolidado, calculado como:

    MC_empresa = (QTDE_ON × PRECO_ON) + (QTDE_PN × PRECO_PN)

Esse é o valor exibido em sites como StatusInvest e Bloomberg.

Dois contextos de teste:
    - `attach_historical_valuation`: MARKET_CAP com PRECO_FIM_ANO (por ano)
    - `build_snapshot_atual`: MARKET_CAP_ATUAL com PRECO_ATUAL (snapshot)
"""
from __future__ import annotations

import polars as pl
import pytest

from synetra.market.price_aggregator import (
    _classify_ticker_class,
    attach_historical_valuation,
    build_snapshot_atual,
)

# --- Fixtures e helpers ---

def _base_row(**overrides: object) -> dict[str, object]:
    """Linha base da série histórica para testes (setor INDUSTRIAL)."""
    base: dict[str, object] = {
        "TICKER": "PETR3",
        "CNPJ_CIA": "33.000.167/0001-01",
        "RAZAO_CVM": "PETROLEO BRASILEIRO S.A.",
        "ANO": 2024,
        "CATEGORIA": "INDUSTRIAL",
        "QTDE_ACOES": 12_888_732_761.0,
        "QTDE_ON": 7_442_231_382.0,
        "QTDE_PN": 5_446_501_379.0,
        "LUCRO_LIQUIDO": 37_000_000_000.0,
        "PATRIMONIO_LIQUIDO": 367_000_000_000.0,
        "RECEITA_LIQUIDA": 500_000_000_000.0,
        "EBITDA": 230_000_000_000.0,
        "DIVIDA_LIQUIDA": 150_000_000_000.0,
        "PRECO_FIM_ANO": 36.19,
    }
    base.update(overrides)
    return base


# --- Classificação de ticker (ON/PN/UNIT) ---

class TestTickerClassClassification:
    """Regra B3: final 3 → ON, final 4/5/6/7/8 → PN, final 11 → UNIT."""

    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("PETR3", "ON"), ("ITUB3", "ON"), ("WEGE3", "ON"),
            ("PETR4", "PN"), ("ITUB4", "PN"), ("BBDC4", "PN"),
            ("CRPG5", "PN"), ("CRPG6", "PN"),
            ("SAPR11", "UNIT"), ("TAEE11", "UNIT"),
        ],
    )
    def test_classificacao_ticker(self, ticker: str, expected: str) -> None:
        df = pl.DataFrame({"TICKER": [ticker]})
        result = df.with_columns(_classify_ticker_class().alias("CLASSE"))
        assert result["CLASSE"][0] == expected


# --- MARKET_CAP histórico (série anual) ---

class TestMarketCapHistorico:
    """MARKET_CAP na série histórica usa PRECO_FIM_ANO."""

    def test_petrobras_consolidado_no_ano(self) -> None:
        """PETR3 e PETR4 em 2024 têm MESMO MC consolidado."""
        history = pl.DataFrame([
            _base_row(TICKER="PETR3", PRECO_FIM_ANO=39.41),
            _base_row(TICKER="PETR4", PRECO_FIM_ANO=36.19),
        ])

        result = attach_historical_valuation(history)
        mc_esperado = 7_442_231_382 * 39.41 + 5_446_501_379 * 36.19

        for row in result.iter_rows(named=True):
            assert row["MARKET_CAP"] == pytest.approx(mc_esperado, rel=0.001)

    def test_mc_varia_entre_anos(self) -> None:
        """MARKET_CAP deve ser diferente quando o preço fim de ano muda."""
        history = pl.DataFrame([
            _base_row(TICKER="PETR3", ANO=2015, PRECO_FIM_ANO=8.57),
            _base_row(TICKER="PETR3", ANO=2022, PRECO_FIM_ANO=28.04),
        ])

        result = attach_historical_valuation(history).sort("ANO")
        mcs = result["MARKET_CAP"].to_list()

        # 2015: preço baixo → MC baixo. 2022: preço subiu → MC maior.
        assert mcs[0] < mcs[1]

    def test_empresa_so_com_on_usa_qtde_on(self) -> None:
        """WEGE3 (só ON, sem PN) → MC = QTDE_ON × PRECO_FIM_ANO."""
        history = pl.DataFrame([_base_row(
            TICKER="WEGE3",
            CNPJ_CIA="14.759.173/0001-49",
            QTDE_ACOES=4_197_317_998.0,
            QTDE_ON=4_197_317_998.0,
            QTDE_PN=0.0,
            PRECO_FIM_ANO=45.52,
        )])

        result = attach_historical_valuation(history)
        mc_esperado = 4_197_317_998 * 45.52
        assert result["MARKET_CAP"][0] == pytest.approx(mc_esperado, rel=0.001)


# --- Snapshot atual (MC_ATUAL) ---

class TestSnapshotMarketCap:
    """MARKET_CAP_ATUAL no snapshot usa o último PRECO_FIM_ANO do histórico."""

    def test_petrobras_mc_atual_bate_com_site(self) -> None:
        """PETR3 e PETR4 mostram mesmo MC_ATUAL (consolidado por empresa)."""
        history = pl.DataFrame([
            _base_row(TICKER="PETR3", PRECO_FIM_ANO=50.11),
            _base_row(TICKER="PETR4", PRECO_FIM_ANO=45.67),
        ])

        snapshot = build_snapshot_atual(history)
        mc_esperado = 7_442_231_382 * 50.11 + 5_446_501_379 * 45.67

        for row in snapshot.iter_rows(named=True):
            assert row["MARKET_CAP_ATUAL"] == pytest.approx(mc_esperado, rel=0.001)

    def test_snapshot_tem_uma_linha_por_ticker(self) -> None:
        """build_snapshot_atual retorna 1 linha por ticker (ano mais recente)."""
        # 16 anos de histórico pra PETR3
        rows = [
            _base_row(TICKER="PETR3", ANO=ano, PRECO_FIM_ANO=30.0)
            for ano in range(2010, 2026)
        ]
        history = pl.DataFrame(rows)

        snapshot = build_snapshot_atual(history)
        assert snapshot.height == 1
        assert snapshot["ANO_REFERENCIA"][0] == 2025

    def test_snapshot_vazio_quando_history_vazio(self) -> None:
        snapshot = build_snapshot_atual(pl.DataFrame())
        assert snapshot.is_empty()


class TestMultiplosAtuaisRefletemPrecoPorClasse:
    """P_L_ATUAL e P_VP_ATUAL refletem preço por classe do último fechamento."""

    def test_pl_atual_diferente_entre_on_e_pn(self) -> None:
        history = pl.DataFrame([
            _base_row(TICKER="PETR3", PRECO_FIM_ANO=50.11),
            _base_row(TICKER="PETR4", PRECO_FIM_ANO=45.67),
        ])

        snapshot = build_snapshot_atual(history).sort("TICKER")
        pls = snapshot["P_L_ATUAL"].to_list()

        # PETR3 tem preço maior → P/L maior
        assert pls[0] > pls[1]

    def test_pvp_atual_diferente_entre_on_e_pn(self) -> None:
        history = pl.DataFrame([
            _base_row(TICKER="PETR3", PRECO_FIM_ANO=50.11),
            _base_row(TICKER="PETR4", PRECO_FIM_ANO=45.67),
        ])

        snapshot = build_snapshot_atual(history).sort("TICKER")
        pvps = snapshot["P_VP_ATUAL"].to_list()

        assert pvps[0] > pvps[1]


# --- Múltiplos históricos na série (P_L, P_VP) ---

class TestMultiplosHistoricosSerie:
    """P_L e P_VP na série histórica usam PRECO_FIM_ANO do próprio ano."""

    def test_pl_usa_preco_fim_ano(self) -> None:
        """P_L = PRECO_FIM_ANO / LPA."""
        # LPA = 100k/10k = 10; P/L = 40/10 = 4.0
        row = {
            "TICKER": "TEST3", "CNPJ_CIA": "00.000.000/0001-00", "ANO": 2020,
            "CATEGORIA": "INDUSTRIAL", "QTDE_ACOES": 10_000.0,
            "QTDE_ON": 10_000.0, "QTDE_PN": 0.0,
            "LUCRO_LIQUIDO": 100_000.0, "PATRIMONIO_LIQUIDO": 500_000.0,
            "RECEITA_LIQUIDA": 200_000.0, "EBITDA": 150_000.0,
            "DIVIDA_LIQUIDA": 50_000.0,
            "PRECO_FIM_ANO": 40.0,
        }
        result = attach_historical_valuation(pl.DataFrame([row]))
        assert result["P_L"][0] == pytest.approx(4.0, rel=0.001)

    def test_pvp_usa_preco_fim_ano(self) -> None:
        """P_VP = PRECO_FIM_ANO / VPA."""
        # VPA = 500k/10k = 50; P/VP = 40/50 = 0.8
        row = {
            "TICKER": "TEST3", "CNPJ_CIA": "00.000.000/0001-00", "ANO": 2020,
            "CATEGORIA": "INDUSTRIAL", "QTDE_ACOES": 10_000.0,
            "QTDE_ON": 10_000.0, "QTDE_PN": 0.0,
            "LUCRO_LIQUIDO": 100_000.0, "PATRIMONIO_LIQUIDO": 500_000.0,
            "RECEITA_LIQUIDA": 200_000.0, "EBITDA": 150_000.0,
            "DIVIDA_LIQUIDA": 50_000.0,
            "PRECO_FIM_ANO": 40.0,
        }
        result = attach_historical_valuation(pl.DataFrame([row]))
        assert result["P_VP"][0] == pytest.approx(0.8, rel=0.001)

    def test_lucro_zero_produz_pl_null(self) -> None:
        row = {
            "TICKER": "TEST3", "CNPJ_CIA": "00.000.000/0001-00", "ANO": 2020,
            "CATEGORIA": "INDUSTRIAL", "QTDE_ACOES": 10_000.0,
            "QTDE_ON": 10_000.0, "QTDE_PN": 0.0,
            "LUCRO_LIQUIDO": 0.0, "PATRIMONIO_LIQUIDO": 500_000.0,
            "RECEITA_LIQUIDA": 200_000.0, "EBITDA": 150_000.0,
            "DIVIDA_LIQUIDA": 50_000.0,
            "PRECO_FIM_ANO": 40.0,
        }
        result = attach_historical_valuation(pl.DataFrame([row]))
        assert result["P_L"][0] is None
