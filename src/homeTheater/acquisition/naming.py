"""Push the target library folder structure to the media stack (plan §5.7 note:
naming lives in Radarr/Sonarr, not in our code — so we *configure* it there).

One idempotent action ("apply naming policy") sets:

* **Radarr** — rename on, ``Movies/<Title (Year)>/<Title (Year) Quality>``;
* **Sonarr** — rename on, season folders,
  ``TV Shows/<Series>/Season 01/<Series - S01E01 - Episode>``;
* **Bazarr** — subtitles into a ``Subs`` subfolder next to each media file
  (movie folder / season folder), for every configured language (he+en by
  default). Best-effort: Bazarr's settings endpoint is less formal than the
  arrs'; a failure is reported, not raised.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx

from ..config import AppConfig
from ..errors import NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from .arr import RadarrClient, SonarrClient
from .service import _radarr, _sonarr

log = get_logger(__name__)


@dataclass
class NamingReport:
    radarr: str = "not configured"
    sonarr: str = "not configured"
    bazarr: str = "not configured"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _apply_radarr(client: RadarrClient, config: AppConfig) -> str:
    org = config.organizer
    naming = await client.naming_config()
    naming.update(
        {
            "renameMovies": True,
            "movieFolderFormat": org.movie_folder_format,
            "standardMovieFormat": org.movie_file_format,
        }
    )
    await client.set_naming_config(naming)
    return f"applied: {org.movie_folder_format}/<file>"


async def _apply_sonarr(client: SonarrClient, config: AppConfig) -> str:
    org = config.organizer
    naming = await client.naming_config()
    naming.update(
        {
            "renameEpisodes": True,
            "seriesFolderFormat": org.series_folder_format,
            "seasonFolderFormat": org.season_folder_format,
            "standardEpisodeFormat": org.episode_file_format,
        }
    )
    await client.set_naming_config(naming)
    return f"applied: {org.series_folder_format}/{org.season_folder_format}/<episode>"


async def _apply_bazarr(config: AppConfig, http: httpx.AsyncClient) -> str:
    secrets = config.secrets
    if not (secrets.bazarr_url and secrets.bazarr_api_key):
        return "not configured"
    org = config.organizer
    # Bazarr stores its settings as form fields on POST /api/system/settings.
    payload = {
        "settings-general-subfolder": "relative",
        "settings-general-subfolder-custom": org.subs_folder,
    }
    resp = await http.post(
        f"{secrets.bazarr_url.rstrip('/')}/api/system/settings",
        data=payload,
        headers={"X-API-KEY": secrets.bazarr_api_key.get_secret_value()},
    )
    resp.raise_for_status()
    return f"applied: subtitles into <media folder>/{org.subs_folder}/"


async def apply_naming_policy(config: AppConfig) -> NamingReport:
    """Configure Radarr/Sonarr naming + Bazarr subtitle placement. Idempotent.

    Only touches services that have credentials; raises NotConfiguredError when
    none do. Applies to titles imported *from now on* (and to existing ones if
    you trigger a rename in the arts' UI).
    """

    report = NamingReport()
    async with httpx.AsyncClient(timeout=20.0) as http:
        radarr = _radarr(config, http)
        sonarr = _sonarr(config, http)
        bazarr_ready = bool(config.secrets.bazarr_url and config.secrets.bazarr_api_key)
        if radarr is None and sonarr is None and not bazarr_ready:
            raise NotConfiguredError(
                "Radarr/Sonarr/Bazarr are not configured in .env — nothing to apply to."
            )
        if radarr is not None:
            try:
                report.radarr = await _apply_radarr(radarr, config)
            except Exception as exc:
                report.radarr = f"failed: {redact_exc(exc)}"
        if sonarr is not None:
            try:
                report.sonarr = await _apply_sonarr(sonarr, config)
            except Exception as exc:
                report.sonarr = f"failed: {redact_exc(exc)}"
        try:
            report.bazarr = await _apply_bazarr(config, http)
        except Exception as exc:
            report.bazarr = f"failed: {redact_exc(exc)}"
    log.info("naming.applied", **report.as_dict())
    return report
