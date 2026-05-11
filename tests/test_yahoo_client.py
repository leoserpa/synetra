"""
Testes do YahooPriceDownloader com API mockada.

Skill: python-testing-patterns → Mocking External Dependencies

Estes testes NÃO batem na API real do Yahoo Finance. Usam monkeypatch
para simular respostas de yfinance.download, garantindo que:

    1. Lógica de sufixo .SA funciona (PETR4 → PETR4.SA)
    2. Cache em Parquet é criado/lido/invalidado corretamente
    3. Batching respeita `batch_size`
    4. Falhas do Yahoo não derrubam o pipeline
    5. Tickers inexistentes são ignorados silenciosamente
    6. DataFrame de saída tem o schema esperado

Isolamento:
    - Todos os testes usam `tmp_path` (cache_dir isolado)
    - Nenhum teste faz rede (yf.download é monkeypatched)
"""
from __future__ import annotations

import time
from unittest.mock import patch

import numpy as np
import pandas as pd
import polars as pl

from synetra.market.yahoo_client import YahooPriceDownloader, _is_nan

# --- Helpers — construção de DataFrame pandas no formato yfinance ---

def _make_yf_multiindex_df(tickers: list[str], dates: list[str]) -> pd.DataFrame:
    """
    Simula o DataFrame que `yf.download(..., group_by='ticker')` retorna
    quando múltiplos tickers são solicitados.

    Estrutura: MultiIndex columns com nível 0 = 'TICKER.SA' e nível 1 = OHLCV.
    """
    columns = pd.MultiIndex.from_product(
        [[f"{t}.SA" for t in tickers], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    # Preenche com dados sintéticos determinísticos
    rows: list[list[float]] = []
    for i, _date in enumerate(dates):
        row: list[float] = []
        for j, _t in enumerate(tickers):
            base = 10.0 + j  # PETR4=10, VALE3=11, etc.
            row.extend([
                base + i,          # Open
                base + i + 0.5,    # High
                base + i - 0.5,    # Low
                base + i + 0.2,    # Close
                base + i + 0.1,    # Adj Close
                1_000_000 + i,     # Volume
            ])
        rows.append(row)

    df = pd.DataFrame(rows, index=pd.to_datetime(dates), columns=columns)
    df.index.name = "Date"
    return df


# --- _is_nan helper ---

class TestIsNan:
    def test_none_is_nan(self) -> None:
        assert _is_nan(None) is True

    def test_python_nan_is_nan(self) -> None:
        assert _is_nan(float("nan")) is True

    def test_numpy_nan_is_nan(self) -> None:
        assert _is_nan(float(np.nan)) is True

    def test_regular_float_is_not_nan(self) -> None:
        assert _is_nan(10.5) is False

    def test_int_is_not_nan(self) -> None:
        assert _is_nan(42) is False

    def test_zero_is_not_nan(self) -> None:
        assert _is_nan(0.0) is False


# --- Cache management ---

class TestCacheLifecycle:
    """Testa criação, leitura e invalidação do cache Parquet."""

    def test_sem_cache_retorna_false(self, tmp_path) -> None:
        """Quando cache_file não existe, `_cache_is_fresh` deve retornar False."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)
        assert dl._cache_is_fresh() is False
        assert dl._load_cache() is None

    def test_salva_e_le_cache(self, tmp_path) -> None:
        """Salva um DataFrame no cache e deve conseguir ler de volta."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)
        df = pl.DataFrame({
            "TICKER": ["PETR4", "VALE3"],
            "DATA": ["2024-01-02", "2024-01-02"],
            "FECHAMENTO": [38.5, 72.1],
        })
        dl._save_cache(df)
        assert dl._cache_is_fresh() is True

        loaded = dl._load_cache()
        assert loaded is not None
        assert loaded.height == 2
        assert "PETR4" in loaded["TICKER"].to_list()

    def test_cache_expirado_retorna_none(self, tmp_path) -> None:
        """Cache com mtime antigo (> cache_max_age_days) deve ser invalidado."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=1)
        df = pl.DataFrame({"TICKER": ["PETR4"], "DATA": ["2024-01-02"], "FECHAMENTO": [38.5]})
        dl._save_cache(df)

        # Envelhece o arquivo artificialmente: 2 dias atrás
        old_mtime = time.time() - (2 * 86400)
        import os
        os.utime(dl.cache_file, (old_mtime, old_mtime))

        assert dl._cache_is_fresh() is False
        assert dl._load_cache() is None

    def test_cache_corrompido_retorna_none_sem_quebrar(self, tmp_path) -> None:
        """Se o Parquet estiver corrompido, retorna None com WARNING — não levanta."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)
        # Escreve lixo no arquivo
        dl.cache_file.write_bytes(b"isso nao eh parquet")
        assert dl._load_cache() is None  # sem exception

    def test_cache_dir_e_criado_automaticamente(self, tmp_path) -> None:
        """O construtor deve criar o cache_dir se não existir."""
        novo = tmp_path / "subpasta_nova"
        assert not novo.exists()
        YahooPriceDownloader(cache_dir=str(novo))
        assert novo.exists()


# --- Download — com yf.download mockado ---

class TestDownloadMocked:
    """Testa o fluxo completo de download SEM bater em rede."""

    def test_download_lista_vazia_retorna_dataframe_vazio(self, tmp_path) -> None:
        """Lista de tickers vazia → DataFrame vazio, sem crash."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))
        with patch("synetra.market.yahoo_client.yf.download", return_value=pd.DataFrame()):
            result = dl.download([], use_cache=False)
        assert isinstance(result, pl.DataFrame)
        assert result.is_empty()

    def test_download_yahoo_vazio_retorna_dataframe_vazio(self, tmp_path) -> None:
        """Yahoo retorna DataFrame vazio → retornamos DataFrame vazio."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))
        with patch("synetra.market.yahoo_client.yf.download", return_value=pd.DataFrame()):
            result = dl.download(["TICKERINEXISTENTE"], use_cache=False)
        assert result.is_empty()

    def test_download_ok_retorna_schema_esperado(self, tmp_path) -> None:
        """Download bem-sucedido deve produzir as 8 colunas esperadas."""
        fake = _make_yf_multiindex_df(["PETR4"], ["2024-01-02", "2024-01-03", "2024-01-04"])
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        with patch("synetra.market.yahoo_client.yf.download", return_value=fake):
            df = dl.download(["PETR4"], inicio="2024-01-01", use_cache=False)

        esperadas = {
            "TICKER", "DATA", "ABERTURA", "MAXIMA", "MINIMA",
            "FECHAMENTO", "FECHAMENTO_AJUSTADO", "VOLUME",
        }
        assert esperadas.issubset(set(df.columns))
        assert df.height == 3
        assert df["TICKER"].unique().to_list() == ["PETR4"]

    def test_download_adiciona_sufixo_sa_na_requisicao(self, tmp_path) -> None:
        """Yahoo exige sufixo .SA — ver se é adicionado na chamada."""
        fake = _make_yf_multiindex_df(["PETR4"], ["2024-01-02"])
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        with patch("synetra.market.yahoo_client.yf.download", return_value=fake) as mock_dl:
            dl.download(["PETR4"], use_cache=False)

        tickers_passados = mock_dl.call_args.args[0]
        assert "PETR4.SA" in tickers_passados

    def test_download_multiplos_tickers_agrupa_corretamente(self, tmp_path) -> None:
        """3 tickers × 2 dias = 6 linhas no resultado."""
        fake = _make_yf_multiindex_df(["PETR4", "VALE3", "ITUB4"], ["2024-01-02", "2024-01-03"])
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        with patch("synetra.market.yahoo_client.yf.download", return_value=fake):
            df = dl.download(["PETR4", "VALE3", "ITUB4"], use_cache=False)

        assert df.height == 6
        assert set(df["TICKER"].unique().to_list()) == {"PETR4", "VALE3", "ITUB4"}

    def test_download_com_exception_no_yahoo_nao_quebra(self, tmp_path) -> None:
        """yf.download lança exception → retornamos DataFrame vazio."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))
        with patch(
            "synetra.market.yahoo_client.yf.download",
            side_effect=ConnectionError("Yahoo fora do ar"),
        ):
            result = dl.download(["PETR4"], use_cache=False)
        assert result.is_empty()

    def test_download_ignora_linhas_com_close_nan(self, tmp_path) -> None:
        """Linhas com Close=NaN são puladas (feriado/sem pregão)."""
        fake = _make_yf_multiindex_df(["PETR4"], ["2024-01-02", "2024-01-03"])
        # Força NaN em Close do dia 2024-01-03
        fake.loc["2024-01-03", ("PETR4.SA", "Close")] = float("nan")
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        with patch("synetra.market.yahoo_client.yf.download", return_value=fake):
            df = dl.download(["PETR4"], use_cache=False)

        assert df.height == 1
        assert df["DATA"][0] == "2024-01-02"

    def test_download_ticker_ausente_na_resposta_do_yahoo_e_ignorado(self, tmp_path) -> None:
        """Se o Yahoo só retorna PETR4 mas pedimos PETR4+INEX9, apenas PETR4 aparece."""
        fake = _make_yf_multiindex_df(["PETR4"], ["2024-01-02"])
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        with patch("synetra.market.yahoo_client.yf.download", return_value=fake):
            df = dl.download(["PETR4", "INEX9"], use_cache=False)

        assert df["TICKER"].unique().to_list() == ["PETR4"]


# --- Batching ---

class TestBatching:
    """Testa que tickers são divididos em batches corretamente."""

    def test_batch_size_divide_chamadas(self, tmp_path) -> None:
        """5 tickers com batch_size=2 → 3 chamadas (2+2+1)."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))

        # Cada chamada retorna MultiIndex só com os tickers do batch
        def fake_download(tickers_yahoo, **kwargs):
            raw = [t.replace(".SA", "") for t in tickers_yahoo]
            return _make_yf_multiindex_df(raw, ["2024-01-02"])

        with patch(
            "synetra.market.yahoo_client.yf.download",
            side_effect=fake_download,
        ) as mock_dl, patch(
            "synetra.market.yahoo_client.time.sleep"  # não dormir entre batches no teste
        ):
            df = dl.download(
                ["T1", "T2", "T3", "T4", "T5"],
                batch_size=2,
                use_cache=False,
            )

        assert mock_dl.call_count == 3
        assert df.height == 5
        assert set(df["TICKER"].unique().to_list()) == {"T1", "T2", "T3", "T4", "T5"}

    def test_batch_size_maior_que_lista_faz_uma_chamada_so(self, tmp_path) -> None:
        """3 tickers com batch_size=100 → 1 chamada só."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path))
        fake = _make_yf_multiindex_df(["A", "B", "C"], ["2024-01-02"])

        with patch(
            "synetra.market.yahoo_client.yf.download",
            return_value=fake,
        ) as mock_dl:
            dl.download(["A", "B", "C"], batch_size=100, use_cache=False)

        assert mock_dl.call_count == 1


# --- Integração cache + download ---

class TestCacheIntegracao:
    """Testa interação entre cache e download."""

    def test_cache_completo_nao_rechama_yahoo(self, tmp_path) -> None:
        """Se o cache cobre todos os tickers solicitados, NÃO chamamos yf.download."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)

        # Pré-popula o cache
        df_cache = pl.DataFrame({
            "TICKER": ["PETR4", "VALE3"],
            "DATA": ["2024-01-02", "2024-01-02"],
            "ABERTURA": [10.0, 11.0],
            "MAXIMA": [10.5, 11.5],
            "MINIMA": [9.5, 10.5],
            "FECHAMENTO": [10.2, 11.2],
            "FECHAMENTO_AJUSTADO": [10.1, 11.1],
            "VOLUME": [1_000_000.0, 2_000_000.0],
        })
        dl._save_cache(df_cache)

        with patch("synetra.market.yahoo_client.yf.download") as mock_dl:
            result = dl.download(["PETR4", "VALE3"], use_cache=True)

        mock_dl.assert_not_called()
        assert result.height == 2

    def test_cache_parcial_rebaixa_tudo(self, tmp_path) -> None:
        """Cache só tem PETR4; pedimos PETR4+VALE3 → rebaixa tudo."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)
        df_cache = pl.DataFrame({
            "TICKER": ["PETR4"],
            "DATA": ["2024-01-02"],
            "ABERTURA": [10.0], "MAXIMA": [10.5], "MINIMA": [9.5],
            "FECHAMENTO": [10.2], "FECHAMENTO_AJUSTADO": [10.1], "VOLUME": [1e6],
        })
        dl._save_cache(df_cache)

        fake = _make_yf_multiindex_df(["PETR4", "VALE3"], ["2024-01-02"])
        with patch(
            "synetra.market.yahoo_client.yf.download", return_value=fake,
        ) as mock_dl:
            dl.download(["PETR4", "VALE3"], use_cache=True)

        mock_dl.assert_called_once()

    def test_use_cache_false_ignora_cache_existente(self, tmp_path) -> None:
        """Com use_cache=False, sempre rebaixa mesmo com cache fresco."""
        dl = YahooPriceDownloader(cache_dir=str(tmp_path), cache_max_age_days=7)
        df_cache = pl.DataFrame({
            "TICKER": ["PETR4"], "DATA": ["2024-01-02"],
            "ABERTURA": [10.0], "MAXIMA": [10.5], "MINIMA": [9.5],
            "FECHAMENTO": [10.2], "FECHAMENTO_AJUSTADO": [10.1], "VOLUME": [1e6],
        })
        dl._save_cache(df_cache)

        fake = _make_yf_multiindex_df(["PETR4"], ["2024-01-02"])
        with patch(
            "synetra.market.yahoo_client.yf.download", return_value=fake,
        ) as mock_dl:
            dl.download(["PETR4"], use_cache=False)

        mock_dl.assert_called_once()
