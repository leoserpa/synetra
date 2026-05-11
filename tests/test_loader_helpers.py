"""Testes unitários para os helpers internos do synetra.loader.

Cobre as funções extraídas na refatoração:
    - _decode_cvm_bytes (decodificação latin1 → utf8)
    - _normalize_doc_type (agrupa DFC_MI/DFC_MD sob "DFC")
    - _dfc_priority (prioridade por tipo de DFC)
    - _apply_monetary_scale (multiplica por 1000 quando MIL)
    - _deduplicate_by_priority (ordena e remove duplicatas por prioridade)
    - _build_fre_output (defaults para QTDE_ON/QTDE_PN ausentes)

Princípios F.I.R.S.T.: fixtures pequenas, sem I/O de rede, sem ZIP real.
"""
from __future__ import annotations

import polars as pl
import pytest

from synetra.loader import (
    _DFC_PRIORITY_MAP,
    _DOC_TYPE_DFC,
    _PRIORITY_CONSOLIDATED,
    _PRIORITY_DFC_DIRECT,
    _PRIORITY_DFC_INDIRECT,
    _PRIORITY_INDIVIDUAL,
    _PRIORITY_NON_DFC,
    _SCALE_FACTOR_MIL,
    _apply_monetary_scale,
    _build_fre_output,
    _cast_fre_columns,
    _decode_cvm_bytes,
    _deduplicate_by_priority,
    _dfc_priority,
    _normalize_doc_type,
    _select_latest_fre_row_per_company,
)

# --- Constantes semânticas ---


class TestPriorityConstants:
    """Constantes de prioridade devem ser consistentes entre si."""

    def test_consolidated_beats_individual(self) -> None:
        assert _PRIORITY_CONSOLIDATED < _PRIORITY_INDIVIDUAL

    def test_dfc_indirect_beats_direct(self) -> None:
        assert _PRIORITY_DFC_INDIRECT < _PRIORITY_DFC_DIRECT

    def test_non_dfc_priority_is_zero(self) -> None:
        assert _PRIORITY_NON_DFC == 0

    def test_scale_factor_is_one_thousand(self) -> None:
        assert pytest.approx(1000.0) == _SCALE_FACTOR_MIL

    def test_dfc_priority_map_is_complete(self) -> None:
        assert _DFC_PRIORITY_MAP == {
            "DFC_MI": _PRIORITY_DFC_INDIRECT,
            "DFC_MD": _PRIORITY_DFC_DIRECT,
        }


# --- _decode_cvm_bytes ---


class TestDecodeCvmBytes:
    """Decodifica latin1 → utf8 sem crashar em bytes inválidos."""

    def test_pure_ascii_is_preserved(self) -> None:
        raw = "Hello World".encode("latin1")
        assert _decode_cvm_bytes(raw) == b"Hello World"

    def test_latin1_special_chars_are_converted(self) -> None:
        raw = "Ação".encode("latin1")
        result = _decode_cvm_bytes(raw)
        assert result.decode("utf8") == "Ação"

    def test_invalid_bytes_are_replaced_not_raised(self) -> None:
        """Bytes corrompidos não devem gerar exceção."""
        raw = b"OK\xffINVALID"
        # Não deve explodir
        result = _decode_cvm_bytes(raw)
        assert isinstance(result, bytes)
        # Caractere inválido vira U+FFFD (REPLACEMENT CHARACTER)
        assert "\ufffd" in result.decode("utf8") or "ÿ" in result.decode("utf8")


# --- _normalize_doc_type ---


class TestNormalizeDocType:
    """DFC_MI e DFC_MD viram "DFC"; demais permanecem."""

    @pytest.mark.parametrize(
        ("input_type", "expected"),
        [
            ("DFC_MI", _DOC_TYPE_DFC),
            ("DFC_MD", _DOC_TYPE_DFC),
            ("DFC", _DOC_TYPE_DFC),
            ("DRE", "DRE"),
            ("BPA", "BPA"),
            ("BPP", "BPP"),
        ],
    )
    def test_normalization_rules(self, input_type: str, expected: str) -> None:
        assert _normalize_doc_type(input_type) == expected


# --- _dfc_priority ---


class TestDfcPriority:
    """Função de prioridade DFC usa o mapa canônico."""

    def test_dfc_indirect_has_highest_priority(self) -> None:
        assert _dfc_priority("DFC_MI") == _PRIORITY_DFC_INDIRECT

    def test_dfc_direct_has_second_priority(self) -> None:
        assert _dfc_priority("DFC_MD") == _PRIORITY_DFC_DIRECT

    def test_non_dfc_returns_zero(self) -> None:
        assert _dfc_priority("DRE") == _PRIORITY_NON_DFC
        assert _dfc_priority("BPA") == _PRIORITY_NON_DFC


# --- _apply_monetary_scale ---


class TestApplyMonetaryScale:
    """Multiplica VL_CONTA por 1000 quando ESCALA_MOEDA contém "MIL"."""

    def test_mil_scale_is_multiplied_by_thousand(self) -> None:
        df = pl.DataFrame(
            {"VL_CONTA": ["150.50"], "ESCALA_MOEDA": ["MIL"]}
        )
        result = _apply_monetary_scale(df)
        assert result["VL_CONTA"][0] == pytest.approx(150500.0)

    def test_mil_is_case_insensitive(self) -> None:
        df = pl.DataFrame(
            {"VL_CONTA": ["100"], "ESCALA_MOEDA": ["mil"]}
        )
        result = _apply_monetary_scale(df)
        assert result["VL_CONTA"][0] == pytest.approx(100_000.0)

    def test_unit_scale_is_unchanged(self) -> None:
        df = pl.DataFrame(
            {"VL_CONTA": ["100"], "ESCALA_MOEDA": ["UNIDADE"]}
        )
        result = _apply_monetary_scale(df)
        assert result["VL_CONTA"][0] == pytest.approx(100.0)

    def test_returns_float_type(self) -> None:
        df = pl.DataFrame({"VL_CONTA": ["100"], "ESCALA_MOEDA": ["MIL"]})
        result = _apply_monetary_scale(df)
        assert result["VL_CONTA"].dtype == pl.Float64


# --- _deduplicate_by_priority ---


class TestDeduplicateByPriority:
    """Consolidado vence Individual; DFC_MI vence DFC_MD; maior versão vence."""

    def test_consolidated_wins_over_individual(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "CD_CONTA": ["1.01", "1.01"],
                "PRIORIDADE_TIPO": [_PRIORITY_INDIVIDUAL, _PRIORITY_CONSOLIDATED],
                "PRIORIDADE_DFC": [0, 0],
                "VL_CONTA": ["individual", "consolidado"],
            }
        )
        result = _deduplicate_by_priority(df)
        assert result.height == 1
        assert result["VL_CONTA"][0] == "consolidado"

    def test_dfc_indirect_wins_over_direct(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "CD_CONTA": ["6.01", "6.01"],
                "PRIORIDADE_TIPO": [_PRIORITY_CONSOLIDATED, _PRIORITY_CONSOLIDATED],
                "PRIORIDADE_DFC": [_PRIORITY_DFC_DIRECT, _PRIORITY_DFC_INDIRECT],
                "VL_CONTA": ["direto", "indireto"],
            }
        )
        result = _deduplicate_by_priority(df)
        assert result.height == 1
        assert result["VL_CONTA"][0] == "indireto"

    def test_newer_version_wins(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "CD_CONTA": ["1.01", "1.01"],
                "PRIORIDADE_TIPO": [_PRIORITY_CONSOLIDATED, _PRIORITY_CONSOLIDATED],
                "PRIORIDADE_DFC": [0, 0],
                "VERSAO": [1, 3],
                "VL_CONTA": ["antiga", "nova"],
            }
        )
        result = _deduplicate_by_priority(df)
        assert result.height == 1
        assert result["VL_CONTA"][0] == "nova"

    def test_priority_columns_are_dropped(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1"],
                "CD_CONTA": ["1.01"],
                "PRIORIDADE_TIPO": [_PRIORITY_CONSOLIDATED],
                "PRIORIDADE_DFC": [0],
                "VL_CONTA": ["x"],
            }
        )
        result = _deduplicate_by_priority(df)
        assert "PRIORIDADE_TIPO" not in result.columns
        assert "PRIORIDADE_DFC" not in result.columns

    def test_different_accounts_both_preserved(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "CD_CONTA": ["1.01", "2.01"],
                "PRIORIDADE_TIPO": [_PRIORITY_CONSOLIDATED, _PRIORITY_CONSOLIDATED],
                "PRIORIDADE_DFC": [0, 0],
                "VL_CONTA": ["x", "y"],
            }
        )
        result = _deduplicate_by_priority(df)
        assert result.height == 2


# --- _cast_fre_columns ---


class TestCastFreColumns:
    """Converte strings em tipos numéricos tratando ON/PN defensivamente."""

    def test_basic_casts_applied(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_Companhia": [" 12.345.678/0001-00 "],
                "Versao": ["2"],
                "Quantidade_Total_Acoes": ["1000000"],
            }
        )
        result = _cast_fre_columns(df)
        assert result["CNPJ_CIA"][0] == "12.345.678/0001-00"
        assert result["Versao"][0] == 2
        assert result["Quantidade_Total_Acoes"][0] == pytest.approx(1_000_000.0)

    def test_on_pn_columns_cast_when_present(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_Companhia": ["CNPJ"],
                "Versao": ["1"],
                "Quantidade_Total_Acoes": ["500"],
                "Quantidade_Acoes_Ordinarias": ["300"],
                "Quantidade_Acoes_Preferenciais": ["200"],
            }
        )
        result = _cast_fre_columns(df)
        assert result["Quantidade_Acoes_Ordinarias"][0] == pytest.approx(300.0)
        assert result["Quantidade_Acoes_Preferenciais"][0] == pytest.approx(200.0)


# --- _select_latest_fre_row_per_company ---


class TestSelectLatestFreRow:
    """Filtra por tipo de capital e escolhe versão mais recente."""

    def test_newer_version_wins_when_types_are_equal(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "Versao": [1, 3],
                "Tipo_Capital": ["Capital Integralizado", "Capital Integralizado"],
                "Quantidade_Total_Acoes": [100.0, 300.0],
            }
        )
        result = _select_latest_fre_row_per_company(df)
        assert result.height == 1
        assert result["Quantidade_Total_Acoes"][0] == pytest.approx(300.0)

    def test_non_capital_types_are_filtered_out(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ2"],
                "Versao": [1, 1],
                "Tipo_Capital": ["Capital Autorizado", "Capital Integralizado"],
                "Quantidade_Total_Acoes": [999.0, 500.0],
            }
        )
        result = _select_latest_fre_row_per_company(df)
        assert result.height == 1
        assert result["CNPJ_CIA"][0] == "CNPJ2"

    def test_without_tipo_capital_column_uses_version_only(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1", "CNPJ1"],
                "Versao": [1, 2],
                "Quantidade_Total_Acoes": [100.0, 200.0],
            }
        )
        result = _select_latest_fre_row_per_company(df)
        assert result.height == 1
        assert result["Quantidade_Total_Acoes"][0] == pytest.approx(200.0)


# --- _build_fre_output ---


class TestBuildFreOutput:
    """Constrói output final com defaults para colunas ON/PN faltantes."""

    def test_includes_on_pn_when_present(self) -> None:
        df = pl.DataFrame(
            {
                "CNPJ_CIA": ["CNPJ1"],
                "Quantidade_Total_Acoes": [1000.0],
                "Quantidade_Acoes_Ordinarias": [700.0],
                "Quantidade_Acoes_Preferenciais": [300.0],
            }
        )
        result = _build_fre_output(df, year=2024)
        assert result["QTDE_ON"][0] == pytest.approx(700.0)
        assert result["QTDE_PN"][0] == pytest.approx(300.0)
        assert result["ANO"][0] == 2024

    def test_null_defaults_when_on_pn_missing(self) -> None:
        df = pl.DataFrame(
            {"CNPJ_CIA": ["CNPJ1"], "Quantidade_Total_Acoes": [1000.0]}
        )
        result = _build_fre_output(df, year=2024)
        assert result["QTDE_ON"][0] is None
        assert result["QTDE_PN"][0] is None
        assert result["QTDE_ACOES"][0] == pytest.approx(1000.0)

    def test_output_columns_order_and_types(self) -> None:
        df = pl.DataFrame(
            {"CNPJ_CIA": ["CNPJ1"], "Quantidade_Total_Acoes": [500.0]}
        )
        result = _build_fre_output(df, year=2023)
        assert result.columns == ["CNPJ_CIA", "ANO", "QTDE_ACOES", "QTDE_ON", "QTDE_PN"]
        assert result["ANO"].dtype == pl.Int32
