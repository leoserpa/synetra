"""Gerenciamento de downloads HTTP e cache local para dados da CVM."""
import io
import time
import logging
import zipfile
import pathlib
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("synetra.downloader")


class CVMDownloader:
    """
    Responsabilidade 1: Acessar URLs e retornar streams de bytes.
    Gerencia o cache local de arquivos Parquet por ano.
    """

    def __init__(self, cache_dir: str = ".synetra_cache"):
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_dfp = self.cache_dir / "years"
        self.cache_dfp.mkdir(exist_ok=True)
        self.cache_fre = self.cache_dir / "fre_years"
        self.cache_fre.mkdir(exist_ok=True)

    def download_zip(self, url: str, retries: int = 3):
        """
        Faz uma requisicao HTTP com timeout e retry automatico.
        Em caso de status 200, retorna um ZipFile em memoria.
        Retorna None em caso de erro apos todas as tentativas.
        """
        for attempt in range(retries):
            try:
                response = requests.get(url, timeout=60)
                if response.status_code == 200:
                    return zipfile.ZipFile(io.BytesIO(response.content))
                if response.status_code == 429:  # Rate limited
                    logger.warning("Rate limited, aguardando... (tentativa %d/%d)", attempt+1, retries)
                    time.sleep(2 ** attempt)
                    continue
                logger.error("Erro ao baixar: Status %d - %s", response.status_code, url)
                return None
            except requests.exceptions.Timeout:
                logger.warning("Timeout (tentativa %d/%d): %s", attempt+1, retries, url)
                time.sleep(2 ** attempt)
            except requests.exceptions.RequestException as e:
                logger.error("Erro de rede: %s", e)
                return None
            except zipfile.BadZipFile as e:
                logger.error("ZIP corrompido: %s", e)
                return None
            except Exception as e:
                logger.error("Erro inesperado: %s: %s", type(e).__name__, e)
                return None
        logger.error("Falha apos %d tentativas: %s", retries, url)
        return None

    def get_cached_years(self, cache_type: str = "dfp") -> list:
        """Retorna lista de anos já cacheados localmente."""
        cache_path = self.cache_dfp if cache_type == "dfp" else self.cache_fre
        return [int(f.stem) for f in cache_path.glob("*.parquet")]

    def get_cache_path(self, year: int, cache_type: str = "dfp") -> pathlib.Path:
        """Retorna o caminho do arquivo Parquet de cache para um dado ano."""
        cache_path = self.cache_dfp if cache_type == "dfp" else self.cache_fre
        return cache_path / f"{year}.parquet"

    def download_years_parallel(self, years: list, url_pattern: str, max_workers: int = 5) -> list:
        """
        Baixa múltiplos anos em paralelo.
        Retorna lista de tuplas (ZipFile | None, year).
        """
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.download_zip, url_pattern.format(year=y)): y
                for y in years
            }
            for future in as_completed(futures):
                year = futures[future]
                zip_obj = future.result()
                results.append((zip_obj, year))
        return results
