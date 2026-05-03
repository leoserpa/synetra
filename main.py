"""
Synetra - Ponto de Entrada Principal
Orquestra os módulos de download, carga e transformação.
"""
import os
import sys
import logging
import polars as pl
from synetra.config import load_config
from synetra.downloader import CVMDownloader
from synetra.loader import process_year_from_zip, process_fre_from_zip, load_parquet_history
from synetra.transformer import FinancialTransformer
from synetra.utils import FundamentusScraper, establish_matches, clean_text_expr


# ============================================================
# Configuração de Logging
# ============================================================
def setup_logging() -> logging.Logger:
    """Configura logging estruturado com output no console e arquivo."""
    logger = logging.getLogger("synetra")
    logger.setLevel(logging.DEBUG)

    # Formato: timestamp + nivel + mensagem
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (INFO+)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler (DEBUG+)
    fh = logging.FileHandler("synetra.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


def main():
    logger = setup_logging()
    logger.info("Synetra v1.0.0 — Iniciando pipeline...")

    # Carregar configuração do TOML (validação Pydantic integrada)
    try:
        config = load_config("parameters.toml")
    except Exception as e:
        logger.critical("Erro na configuração: %s", e)
        return

    years = range(config["pipeline"]["years_start"], config["pipeline"]["years_end"])
    max_workers = config["pipeline"]["max_workers"]

    # Inicializar módulos
    downloader = CVMDownloader()
    transformer = FinancialTransformer(config)

    # ============================================================
    # 1. Obter Mapeamento Ticker-CNPJ (Prioridade: Arquivo Local)
    # ============================================================
    mapa_file = 'mapa_tickers.csv'
    if os.path.exists(mapa_file):
        logger.info("Lendo mapeamento Ticker-CNPJ a partir de %s (Modo Offline-First)", mapa_file)
        try:
            df_matches = pl.read_csv(mapa_file, separator=';', infer_schema_length=0, encoding='utf8-lossy')
        except Exception as e:
            logger.warning("Erro ao ler mapa em utf8: %s. Tentando latin1...", e)
            df_matches = pl.read_csv(mapa_file, separator=';', infer_schema_length=0, encoding='latin1')

        # Limpeza nativa (sem map_elements)
        df_matches = df_matches.with_columns(clean_text_expr('RAZAO_CVM').alias('RAZAO_CVM'))
    else:
        logger.info("Mapa local não encontrado. Iniciando busca no Fundamentus...")
        fund = FundamentusScraper()
        df_fund = fund.get_tickers()

        logger.info("Lendo dados recentes da CVM para realizar o Mapeamento...")
        # Download apenas do ano mais recente para o match
        url_pattern = config["urls"]["dfp_pattern"]
        zip_obj = downloader.download_zip(url_pattern.format(year=2024))
        if zip_obj:
            df_latest = process_year_from_zip(zip_obj, 2024, config["pipeline"]["doc_types"])
        else:
            raise RuntimeError("Não foi possível baixar os dados para o mapeamento.")

        logger.info("Gerando mapeamento Ticker-CNPJ inicial (Fuzzy Match)...")
        threshold = config["fuzzy_match"]["threshold"]
        df_matches = establish_matches(df_latest, df_fund, threshold=threshold)

        # Salvar mapa para uso offline
        df_matches.write_csv(mapa_file, separator=';')
        logger.info("Mapeamento salvo em %s. Você pode auditar e editar este arquivo!", mapa_file)

    cnpjs_alvo = df_matches.select('CNPJ_CIA').unique().to_series().to_list()

    # ============================================================
    # 2. Download e Carga de DFP (com cache inteligente)
    # ============================================================
    logger.info("Carregando Histórico CVM DFP...")
    anos_cached = downloader.get_cached_years("dfp")
    anos_faltantes = [y for y in years if y not in anos_cached]

    if anos_faltantes:
        logger.info("Faltam %d anos no cache. Baixando em paralelo...", len(anos_faltantes))
        url_pattern = config["urls"]["dfp_pattern"]
        results = downloader.download_years_parallel(anos_faltantes, url_pattern, max_workers)

        for zip_obj, year in results:
            if zip_obj is not None:
                logger.info("Processando Ano: %d...", year)
                df_ano = process_year_from_zip(zip_obj, year, config["pipeline"]["doc_types"])
                if not df_ano.is_empty():
                    cache_path = downloader.get_cache_path(year, "dfp")
                    df_ano.write_parquet(cache_path, compression="zstd")
                    logger.debug("Cache do ano %d salvo em %s", year, cache_path)
    else:
        logger.info("Todos os anos já estão no cache. Nenhum download novo necessário.")

    logger.info("Lendo cache local de dados brutos (Lazy Parquet Scan)...")
    dfp_files = [
        str(downloader.get_cache_path(y, "dfp"))
        for y in years if downloader.get_cache_path(y, "dfp").exists()
    ]
    df_raw_history = load_parquet_history(dfp_files, filter_cnpjs=cnpjs_alvo)

    # ============================================================
    # 3. Download e Carga de FRE (Ações)
    # ============================================================
    logger.info("Carregando Histórico CVM FRE (Ações)...")
    fre_cached = downloader.get_cached_years("fre")
    fre_faltantes = [y for y in years if y not in fre_cached]

    if fre_faltantes:
        logger.info("Faltam %d anos no cache de Ações (FRE). Baixando...", len(fre_faltantes))
        fre_url = config["urls"]["fre_pattern"]
        fre_results = downloader.download_years_parallel(fre_faltantes, fre_url, max_workers)

        for zip_obj, year in fre_results:
            if zip_obj is not None:
                df_fre_ano = process_fre_from_zip(zip_obj, year)
                if not df_fre_ano.is_empty():
                    cache_path = downloader.get_cache_path(year, "fre")
                    df_fre_ano.write_parquet(cache_path, compression="zstd")

    fre_files = [
        str(downloader.get_cache_path(y, "fre"))
        for y in years if downloader.get_cache_path(y, "fre").exists()
    ]
    df_fre_history = load_parquet_history(fre_files, filter_cnpjs=cnpjs_alvo)

    # ============================================================
    # 4. Transformação e Cálculo de Indicadores
    # ============================================================
    df_final = transformer.calculate_indicators(df_raw_history, df_matches, df_fre_history=df_fre_history)

    # ============================================================
    # 5. Exportação
    # ============================================================
    df_final.write_csv("serie_historica_financeira.csv", separator=';', float_precision=2)
    logger.info("SUCESSO: Série Histórica CVM gerada (%d linhas).", df_final.height)

    # ============================================================
    # 6. Auditoria Automática de Qualidade (Financial Auditor)
    # ============================================================
    logger.info("Auditoria de Qualidade dos Dados...")

    # 6a. Outliers de ROE (> 500% ou < -500%)
    if 'ROE' in df_final.columns:
        outliers_roe = df_final.filter(pl.col('ROE').abs() > 5.0)
        if outliers_roe.height > 0:
            logger.warning("OUTLIER: %d registros com ROE fora do intervalo [-500%%, +500%%].", outliers_roe.height)
        else:
            logger.info("ROE: Nenhum outlier detectado.")

    # 6b. Gaps Temporais (anos faltando por Ticker)
    tickers = df_final.select('TICKER').unique().to_series().to_list()
    gaps_found = 0
    for ticker in tickers[:10]:  # Amostra dos 10 primeiros
        anos_ticker = df_final.filter(pl.col('TICKER') == ticker).select('ANO').to_series().sort().to_list()
        if len(anos_ticker) >= 2:
            expected = list(range(min(anos_ticker), max(anos_ticker) + 1))
            missing = set(expected) - set(anos_ticker)
            if missing:
                gaps_found += 1
    if gaps_found > 0:
        logger.warning("GAPS: %d/10 tickers amostrados possuem gaps temporais.", gaps_found)
    else:
        logger.info("Continuidade Temporal: Sem gaps detectados na amostra.")

    # 6c. Empresas sem Receita Líquida
    if 'RECEITA_LIQUIDA' in df_final.columns:
        sem_receita = df_final.filter(
            (pl.col('RECEITA_LIQUIDA') == 0) | pl.col('RECEITA_LIQUIDA').is_null()
        )
        pct = round(sem_receita.height / max(df_final.height, 1) * 100, 1)
        logger.info("%s%% dos registros possuem Receita Líquida = 0 ou nula (esperado para Bancos).", pct)

    logger.info("Auditoria concluída.")

    # Exibicao 100% Polars (sem dependencia de Pandas para visualizacao)
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        with pl.Config(tbl_cols=-1, tbl_rows=10, fmt_float="full"):
            print(df_final.head(10))
    except Exception:
        logger.info("Exibição em tabela indisponível no terminal. Veja o arquivo CSV gerado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.getLogger("synetra").critical("ERRO FATAL: %s", e, exc_info=True)
