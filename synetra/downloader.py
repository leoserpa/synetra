"""Downloads HTTP assíncronos e cache local de arquivos ZIP da CVM.

Responsabilidades:
    1. Verificar se o cache local está atualizado via HEAD + Last-Modified.
    2. Baixar múltiplos anos em paralelo com limite de concorrência.
    3. Manter caminhos previsíveis para Parquets cacheados (DFP e FRE).

Decisões de projeto:
    - Timeouts curtos para HEAD (só metadata), longos para GET (arquivo ZIP).
    - Fail-open em falhas de verificação: assume cache válido se rede falhar,
      priorizando resiliência do pipeline sobre precisão absoluta da atualização.
"""
from __future__ import annotations

import asyncio
import io
import pathlib
import zipfile
from collections.abc import Iterable
from email.utils import parsedate_to_datetime
from typing import Final, Literal

import httpx
from loguru import logger

# --- Type aliases ---

#: Tipo literal para diferenciar caches DFP (Demonstrações Financeiras Padronizadas)
#: e FRE (Formulário de Referência — dados de capital social/ações).
CacheType = Literal["dfp", "fre"]

#: Resultado de um download único: tupla ``(ZipFile aberto ou None em falha, ano)``.
DownloadResult = tuple[zipfile.ZipFile | None, int]


# --- Constantes de configuração HTTP ---

#: Timeout curto para HEAD — só metadata, CVM costuma responder rápido.
_HEAD_TIMEOUT_SECONDS: Final[float] = 15.0

#: Timeout longo para GET — ZIPs podem ter 50+ MB, conexão brasileira lenta.
_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 60.0

#: Retries em HEAD (reduzido, falha rápido para validação).
_HEAD_RETRIES: Final[int] = 2

#: Retries em GET (maior, arquivo grande vale a pena re-tentar).
_DOWNLOAD_RETRIES: Final[int] = 3

#: Limite default de downloads simultâneos (politeness com a CVM).
_DEFAULT_MAX_WORKERS: Final[int] = 5

#: Status HTTP que indica sucesso completo (arquivo disponível).
_HTTP_OK: Final[int] = 200

# --- Constantes de filesystem ---

#: Subdiretório do cache para ZIPs DFP (Demonstrações Financeiras Padronizadas).
_DFP_SUBDIR: Final[str] = "years"

#: Subdiretório do cache para ZIPs FRE (Capital Social / Formulário de Referência).
_FRE_SUBDIR: Final[str] = "fre_years"

#: Header HTTP consultado para detectar atualização no servidor.
_LAST_MODIFIED_HEADER: Final[str] = "Last-Modified"


# --- Helpers internos ---


def _server_is_newer_than_cache(
    response: httpx.Response, cache_file: pathlib.Path
) -> bool:
    """Retorna ``True`` se o servidor tem versão mais recente que o cache local.

    Args:
        response: Resposta HEAD do servidor CVM.
        cache_file: Arquivo local a comparar.

    Returns:
        ``True`` se ``Last-Modified`` do servidor > ``mtime`` do cache.
        ``False`` se o header estiver ausente, status ≠ 200, ou se o cache
        for mais recente (caso normal de cache válido).
    """
    if response.status_code != _HTTP_OK:
        return False
    if _LAST_MODIFIED_HEADER not in response.headers:
        return False

    server_time = parsedate_to_datetime(
        response.headers[_LAST_MODIFIED_HEADER]
    ).timestamp()
    local_time = cache_file.stat().st_mtime
    return server_time > local_time


def _build_zip_from_response(response: httpx.Response) -> zipfile.ZipFile:
    """Constrói um ``ZipFile`` em memória a partir do corpo da resposta HTTP."""
    return zipfile.ZipFile(io.BytesIO(response.content))


# --- CVMDownloader ---


class CVMDownloader:
    """Gerencia downloads assíncronos da CVM e cache local de Parquets.

    Attributes:
        cache_dir: Diretório raiz do cache (padrão: ``.synetra_cache``).
        cache_dfp: Subdiretório para Parquets DFP (anos).
        cache_fre: Subdiretório para Parquets FRE (capital social).
    """

    def __init__(self, cache_dir: str = ".synetra_cache") -> None:
        self.cache_dir: pathlib.Path = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        self.cache_dfp: pathlib.Path = self.cache_dir / _DFP_SUBDIR
        self.cache_dfp.mkdir(exist_ok=True)

        self.cache_fre: pathlib.Path = self.cache_dir / _FRE_SUBDIR
        self.cache_fre.mkdir(exist_ok=True)

    # --- API pública ---

    def get_cache_path(
        self, year: int, cache_type: CacheType = "dfp"
    ) -> pathlib.Path:
        """Retorna o caminho do Parquet em cache para um ano e tipo.

        Args:
            year: Ano do exercício (ex: ``2024``).
            cache_type: ``"dfp"`` para Demonstrações ou ``"fre"`` para capital social.

        Returns:
            Caminho completo para o arquivo ``.parquet`` (pode não existir ainda).
        """
        base = self.cache_dfp if cache_type == "dfp" else self.cache_fre
        return base / f"{year}.parquet"

    async def get_missing_years(
        self,
        years: Iterable[int],
        url_pattern: str,
        cache_type: CacheType = "dfp",
        max_workers: int = _DEFAULT_MAX_WORKERS,  # noqa: ARG002 (mantido por retrocompat)
    ) -> list[int]:
        """Retorna anos com cache ausente ou desatualizado.

        Verificação paralela via HEAD + ``Last-Modified`` (não baixa o ZIP).

        Args:
            years: Anos a verificar (ex: ``range(2010, 2026)``).
            url_pattern: URL template contendo ``{year}`` (ex:
                ``"https://.../{year}.zip"``).
            cache_type: ``"dfp"`` ou ``"fre"``.
            max_workers: Aceito por retrocompatibilidade. O httpx gerencia
                seu próprio pool internamente; este parâmetro é ignorado.

        Returns:
            Lista dos anos que precisam ser baixados.
        """
        transport = httpx.AsyncHTTPTransport(retries=_HEAD_RETRIES)

        async with httpx.AsyncClient(
            transport=transport, timeout=_HEAD_TIMEOUT_SECONDS
        ) as client:
            tasks = [
                self._check_year_freshness(client, year, url_pattern, cache_type)
                for year in years
            ]
            results = await asyncio.gather(*tasks)
            return [y for y in results if y is not None]

    async def download_years_parallel(
        self,
        years: Iterable[int],
        url_pattern: str,
        max_workers: int = _DEFAULT_MAX_WORKERS,
    ) -> list[DownloadResult]:
        """Baixa múltiplos anos simultaneamente com ``asyncio.gather`` + Semaphore.

        Args:
            years: Anos a baixar.
            url_pattern: URL template contendo ``{year}``.
            max_workers: Número máximo de downloads concorrentes.

        Returns:
            Lista de tuplas ``(ZipFile | None, year)`` na ordem dos anos.
        """
        semaphore = asyncio.Semaphore(max_workers)
        transport = httpx.AsyncHTTPTransport(retries=_DOWNLOAD_RETRIES)

        async with httpx.AsyncClient(
            transport=transport, timeout=_DOWNLOAD_TIMEOUT_SECONDS
        ) as client:
            tasks = [
                self._download_single(
                    client, url_pattern.format(year=y), y, semaphore
                )
                for y in years
            ]
            return await asyncio.gather(*tasks)

    # --- Etapas privadas — verificação de frescor ---

    async def _check_year_freshness(
        self,
        client: httpx.AsyncClient,
        year: int,
        url_pattern: str,
        cache_type: CacheType,
    ) -> int | None:
        """Retorna o ano se ele precisa ser rebaixado, ``None`` se cache válido."""
        url = url_pattern.format(year=year)
        cache_file = self.get_cache_path(year, cache_type)
        is_valid = await self._is_cache_valid(client, url, cache_file)
        return None if is_valid else year

    async def _is_cache_valid(
        self,
        client: httpx.AsyncClient,
        url: str,
        cache_file: pathlib.Path,
    ) -> bool:
        """Valida cache via HEAD request contra ``Last-Modified`` do servidor.

        Args:
            client: Cliente HTTP async reutilizável.
            url: URL original do ZIP na CVM.
            cache_file: Caminho do Parquet local.

        Returns:
            ``True`` se o cache é válido (ou não pôde ser verificado);
            ``False`` se servidor tem versão mais nova.

        Política de erro (fail-open):
            - Timeout e erros de rede → retorna ``True`` (prefere cache local
              a derrubar o pipeline). Logs em WARNING para o operador saber.
        """
        if not cache_file.exists():
            return False

        try:
            response = await client.head(url, follow_redirects=True)
        except httpx.TimeoutException:
            logger.warning(
                "Timeout ao validar cache {} — usando cache local "
                "(pode estar desatualizado)",
                cache_file.name,
            )
            return True
        except (httpx.HTTPError, httpx.RequestError, httpx.InvalidURL) as exc:
            logger.warning(
                "Erro ao validar cache {} ({}) — usando cache local",
                cache_file.name, exc,
            )
            return True

        if _server_is_newer_than_cache(response, cache_file):
            logger.info(
                "Atualização na CVM detectada para {}. Cache invalidado.",
                cache_file.name,
            )
            return False
        return True

    # --- Etapas privadas — download efetivo ---

    async def _download_single(
        self,
        client: httpx.AsyncClient,
        url: str,
        year: int,
        semaphore: asyncio.Semaphore,
    ) -> DownloadResult:
        """Baixa um único ZIP respeitando o semáforo de concorrência.

        Returns:
            Tupla ``(ZipFile, year)`` em sucesso; ``(None, year)`` em falha.
        """
        async with semaphore:
            response = await self._fetch_zip_response(client, url)
            if response is None:
                return None, year
            return _build_zip_from_response(response), year

    @staticmethod
    async def _fetch_zip_response(
        client: httpx.AsyncClient, url: str
    ) -> httpx.Response | None:
        """Faz GET do ZIP, retornando ``None`` em caso de erro de rede/HTTP.

        Loga separadamente:
            - Timeout → WARNING (pode ser transitório).
            - Status != 200 → ERROR (resposta do servidor com problema).
            - Erros de rede → ERROR (conexão/URL inválida).
        """
        try:
            response = await client.get(url, follow_redirects=True)
        except httpx.TimeoutException:
            logger.warning("Timeout ao baixar {}", url)
            return None
        except (httpx.HTTPError, httpx.RequestError, httpx.InvalidURL) as exc:
            logger.error("Erro de rede ao baixar {}: {}", url, exc)
            return None

        if response.status_code != _HTTP_OK:
            logger.error("Erro {} ao baixar {}", response.status_code, url)
            return None
        return response
