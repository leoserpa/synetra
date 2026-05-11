"""
Synetra - Ponto de Entrada Principal
Orquestra os módulos de download, carga e transformação.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import polars as pl
from loguru import logger

from synetra import __version__
from synetra.config import load_config
from synetra.data_quality import run_data_quality_audit
from synetra.downloader import CVMDownloader
from synetra.loader import (
    get_sectors,
    load_parquet_history,
    process_fre_from_zip,
    process_year_from_zip,
)
from synetra.market import (
    YahooPriceDownloader,
    attach_historical_valuation,
    attach_prices_to_history,
    build_snapshot_atual,
)
from synetra.observability import PipelineMetrics, timed_step
from synetra.transformer import FinancialTransformer
from synetra.utils import clean_text_expr, display_df_preview


def setup_logging() -> None:
    """Configura logging estruturado com output no console e arquivo."""
    logger.remove()
    fmt = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>"
    logger.add(sys.stdout, format=fmt, level="INFO", colorize=True)
    logger.add("synetra.log", format=fmt, level="DEBUG", rotation="10 MB", encoding="utf-8")


async def load_and_cache_years(
    downloader: CVMDownloader,
    years: range,
    url_pattern: str,
    cache_type: str,
    doc_types: list[str],
    max_workers: int,
) -> list[str]:
    """Baixa anos ausentes e retorna o histórico consolidado via Lazy Parquet Scan.

    Otimização: processamento de ZIPs em paralelo usando thread pool,
    pois a leitura/parsing é CPU-bound (não se beneficia do asyncio).
    """
    anos_faltantes = await downloader.get_missing_years(years, url_pattern, cache_type=cache_type, max_workers=max_workers)

    if anos_faltantes:
        logger.info("Faltam {} anos ({}). Baixando em paralelo...", len(anos_faltantes), cache_type.upper())
        results = await downloader.download_years_parallel(anos_faltantes, url_pattern, max_workers)

        # Processa os ZIPs em paralelo (CPU-bound: extração + parsing CSV)
        loop = asyncio.get_running_loop()

        def _process_and_save(zip_obj, year: int) -> None:
            """Função CPU-bound executada em thread pool."""
            try:
                if cache_type == "dfp":
                    df_ano = process_year_from_zip(zip_obj, year, doc_types)
                else:
                    df_ano = process_fre_from_zip(zip_obj, year)

                if not df_ano.is_empty():
                    cache_path = downloader.get_cache_path(year, cache_type)
                    df_ano.write_parquet(cache_path, compression="zstd")
            finally:
                zip_obj.close()

        tasks = [
            loop.run_in_executor(None, _process_and_save, zip_obj, year)
            for zip_obj, year in results
            if zip_obj is not None
        ]
        if tasks:
            await asyncio.gather(*tasks)
    else:
        logger.info("Smart Cache ({}): Tudo sincronizado.", cache_type.upper())

    files: list[str] = [
        str(downloader.get_cache_path(y, cache_type))
        for y in years if downloader.get_cache_path(y, cache_type).exists()
    ]
    return files


async def main() -> None:
    """Ponto de entrada principal do pipeline Synetra."""
    start_time = time.perf_counter()
    setup_logging()
    logger.info("Synetra v{}. Iniciando pipeline.", __version__)

    # Observabilidade: agregador de métricas do pipeline
    metrics = PipelineMetrics()
    metrics.set_metadata("version", __version__)
    metrics.set_metadata("started_at", time.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        config = load_config("config.toml")
    except Exception:
        logger.opt(exception=True).critical("Erro na configuração. Abortando.")
        raise SystemExit(1) from None

    years = range(config.pipeline.years_start, config.pipeline.years_end)
    max_workers = config.pipeline.max_workers
    doc_types = config.pipeline.doc_types

    downloader = CVMDownloader()
    transformer = FinancialTransformer(config)

    # Lógica de Force Refresh: Limpa o cache de processamento (Parquets) se solicitado
    if getattr(config.pipeline, 'force_refresh', False):
        cache_dir = pathlib.Path(".synetra_cache/years")
        if cache_dir.exists():
            logger.warning("FORCE REFRESH: Limpando cache de processamento por solicitação do config.toml...")
            for f in cache_dir.glob("*.parquet"):
                f.unlink()

    # 1. Mapeamento Ticker-CNPJ
    mapa_file = pathlib.Path('mapa_tickers.csv')
    if not mapa_file.exists():
        logger.critical("Arquivo '{}' não encontrado. Obrigatório (CNPJ_CIA;TICKER;RAZAO_CVM).", mapa_file)
        return

    logger.info("Lendo mapeamento Ticker-CNPJ a partir de {}", mapa_file)
    df_matches = pl.read_csv(mapa_file, separator=';', infer_schema_length=0, encoding='utf8-lossy')

    df_matches = df_matches.with_columns(clean_text_expr('RAZAO_CVM').alias('RAZAO_CVM'))
    cnpjs_alvo = df_matches.select('CNPJ_CIA').unique().to_series().to_list()

    # 2. Download e Carga de DFP
    logger.info("Carregando Histórico CVM DFP...")
    with timed_step("download_and_cache_dfp", metrics):
        dfp_files = await load_and_cache_years(
            downloader, years, config.urls.dfp_pattern, "dfp", doc_types, max_workers
        )
    with timed_step("load_dfp_parquets", metrics):
        df_raw_history = load_parquet_history(dfp_files, filter_cnpjs=cnpjs_alvo)

    # 3. Download e Carga de FRE (Ações)
    logger.info("Carregando Histórico CVM FRE (Ações)...")
    with timed_step("download_and_cache_fre", metrics):
        fre_files = await load_and_cache_years(
            downloader, years, config.urls.fre_pattern, "fre", doc_types, max_workers
        )
    with timed_step("load_fre_parquets", metrics):
        df_fre_history = load_parquet_history(fre_files, filter_cnpjs=cnpjs_alvo)

    # 4. Setores Econômicos (Cadastro CVM)
    logger.info("Obtendo Setores Econômicos...")
    with timed_step("get_sectors", metrics):
        df_setor = get_sectors(config.urls.cadastro)

    # 5. Transformação e Cálculo de Indicadores
    with timed_step("calculate_indicators", metrics):
        df_final = transformer.calculate_indicators(
            df_raw_history, df_matches, df_setor, df_fre_history=df_fre_history
        )

    # 5.5. Integração com Yahoo Finance (cotações) — opcional via config
    if config.market.enabled:
        logger.info("Baixando cotações do Yahoo Finance...")
        with timed_step("market_download_prices", metrics):
            yahoo = YahooPriceDownloader(
                cache_max_age_days=config.market.cache_max_age_days,
            )
            # Extrai tickers únicos e não-nulos do mapa
            tickers_mapa = (
                df_matches.select("TICKER")
                .filter(pl.col("TICKER").is_not_null() & (pl.col("TICKER").str.len_chars() > 0))
                .unique()
                .to_series()
                .to_list()
            )
            anos_ini = f"{config.pipeline.years_start}-01-01"
            df_precos = yahoo.download(
                tickers_mapa,
                inicio=anos_ini,
                use_cache=True,
                batch_size=config.market.batch_size,
            )

        with timed_step("market_attach_prices", metrics):
            df_final = attach_prices_to_history(df_final, df_precos)

        # Múltiplos HISTÓRICOS anuais (P_L, P_VP, MARKET_CAP usando PRECO_FIM_ANO)
        with timed_step("market_attach_historical_valuation", metrics):
            df_final = attach_historical_valuation(df_final)

        # Métricas de cobertura de mercado
        com_preco = df_final.filter(pl.col("PRECO_FIM_ANO").is_not_null()).height
        metrics.record("market_coverage_rows", com_preco)
        metrics.record("market_coverage_pct", round(com_preco / df_final.height * 100, 1))

        # Snapshot: usa o último fechamento diário do Yahoo (pregão mais recente)
        with timed_step("market_build_snapshot", metrics):
            df_snapshot = build_snapshot_atual(df_final, df_precos)

        com_atual = df_snapshot.filter(pl.col("PRECO_ATUAL").is_not_null()).height if not df_snapshot.is_empty() else 0
        metrics.record("snapshot_tickers", df_snapshot.height)
        metrics.record("snapshot_coverage_rows", com_atual)
        metrics.record(
            "snapshot_coverage_pct",
            round(com_atual / df_snapshot.height * 100, 1) if df_snapshot.height else 0,
        )
    else:
        logger.info("Yahoo Finance desabilitado no config.toml [market].")
        df_snapshot = pl.DataFrame()

    # 6. Exportação
    with timed_step("export_csv", metrics):
        df_final.write_csv("serie_historica_financeira.csv", separator=';', float_precision=2)
        if not df_snapshot.is_empty():
            df_snapshot.write_csv("snapshot_atual.csv", separator=';', float_precision=2)
            logger.info("Snapshot atual gerado: {} tickers.", df_snapshot.height)
    logger.info("Série histórica CVM gerada: {} linhas.", df_final.height)

    # Registra contadores de negócio
    metrics.record("rows_generated", df_final.height)
    metrics.record("columns_generated", df_final.width)
    metrics.record("unique_tickers", df_final.select('TICKER').n_unique())

    # 7. Auditoria de Qualidade
    with timed_step("audit_data", metrics):
        report = transformer.audit_data(df_final)

    logger.info("--- RELATÓRIO DE AUDITORIA ---")
    if report['gaps_count'] > 0:
        logger.warning("GAPS: {} anos faltando em {} empresas.", report['gaps_count'], report['tickers_with_gaps'])
    else:
        logger.info("QUALIDADE: Integridade temporal OK.")

    if report.get('roe_outliers', 0) > 0:
        logger.warning("OUTLIERS: {} registros com ROE extremo (>500%).", report['roe_outliers'])

    logger.info("RECEITA: {}% registros com Receita Zero.", report['zero_revenue_pct'])
    logger.info("-------------------------------")

    # Registra métricas da auditoria
    metrics.record("temporal_gaps", report['gaps_count'])
    metrics.record("roe_outliers", report.get('roe_outliers', 0))
    metrics.record("zero_revenue_pct", report.get('zero_revenue_pct', 0.0))

    # 8. Data Quality Audit (nao-destrutivo — gera data_quality_report.csv)
    if config.market.enabled and not df_snapshot.is_empty():
        with timed_step("data_quality_audit", metrics):
            dq_report = run_data_quality_audit(
                df_history=df_final,
                df_snapshot=df_snapshot,
            )
        metrics.record("dq_ok", dq_report.ok_count)
        metrics.record("dq_low", dq_report.low_count)
        metrics.record("dq_medium", dq_report.medium_count)
        metrics.record("dq_high", dq_report.high_count)

    # Exibicao 100% Polars (sem dependencia de Pandas para visualizacao)
    display_df_preview(df_final)

    elapsed_time = time.perf_counter() - start_time
    logger.info("Pipeline concluído em {:.2f}s.", elapsed_time)

    # Emite sumário estruturado de métricas
    metrics.log_summary()


if __name__ == "__main__":
    try:
        # Ponto de entrada assíncrono para iniciar o loop de eventos
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception:
        logger.opt(exception=True).critical("ERRO FATAL")
        raise SystemExit(1) from None
