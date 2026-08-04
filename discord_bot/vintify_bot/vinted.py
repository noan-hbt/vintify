"""Service Vinted asynchrone autour du module synchrone ``vinted_scraper``.

Le module est synchrone et partage une session HTTP : toutes les appels
passent par ``asyncio.to_thread`` et sont sérialisés par un verrou global,
ce qui respecte le throttle interne et évite tout accès concurrent.

Aucun cookie de session n'est fourni : seuls les endpoints publics sont
utilisés (recherche, détail d'article, données de référence).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from vinted_scraper import VintedScraper

log = logging.getLogger("vintify.vinted")


def _extract_brands(payload: Any) -> list[dict[str, Any]]:
    """Extrait la liste ``brands`` d'une réponse ``/api/v2/brands``."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        brands = payload.get("brands")
        if isinstance(brands, list):
            return brands
    return []

# Libellés français pour les états d'annonces les plus courants
STATUS_FR = {
    "new_with_tags": "Neuf avec étiquettes",
    "new_without_tags": "Neuf sans étiquettes",
    "very_good": "Très bon état",
    "good": "Bon état",
    "satisfactory": "État satisfaisant",
    "not_specified": "Non précisé",
}


def status_title(status: Any) -> str:
    """Traduit un identifiant d'état Vinted en libellé français lisible."""
    if not status:
        return "Non précisé"
    text = str(status)
    return STATUS_FR.get(text, text)


class VintedService:
    """Wrapper asynchrone, avec cache des marques et compteur de requêtes."""

    def __init__(self, domain: str, page_size: int = 24):
        self.domain = domain
        self.page_size = page_size
        self.requests = 0
        self._scraper: VintedScraper | None = None
        self._lock = asyncio.Lock()
        self._brands: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ #
    # Cycle de vie
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Crée le client (le bootstrap récupère cookies + token CSRF)."""
        await self._scraper_or_start()

    async def close(self) -> None:
        if self._scraper is not None:
            await asyncio.to_thread(self._scraper.close)
            self._scraper = None

    async def _scraper_or_start(self) -> VintedScraper:
        if self._scraper is None:
            async with self._lock:
                if self._scraper is None:
                    log.info("Initialisation du client Vinted (domaine %s)…", self.domain)
                    self._scraper = await asyncio.to_thread(
                        VintedScraper,
                        domain=self.domain,
                        timeout=30,
                        retries=4,
                        min_request_interval=0.25,
                        verbose=False,
                    )
        return self._scraper

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Exécute une méthode synchrone du scraper dans un thread, sérialisé."""
        async with self._lock:
            self.requests += 1
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ------------------------------------------------------------------ #
    # Catalogue
    # ------------------------------------------------------------------ #

    async def search(self, filters: dict[str, Any], page_size: int | None = None) -> list[dict[str, Any]]:
        """Page la plus récente d'une recherche (ids normalisés, tri nouveau d'abord)."""
        scraper = await self._scraper_or_start()
        size = page_size or self.page_size
        items = await self._call(
            scraper.iter_items,
            filters=filters,
            pages=1,
            page_size=size,
            normalize=True,
        )
        return list(items)

    async def get_item(self, item_id: int | str) -> dict[str, Any]:
        """Détail normalisé d'un article."""
        scraper = await self._scraper_or_start()
        return await self._call(scraper.get_item, item_id, normalize=True)

    # ------------------------------------------------------------------ #
    # Marques (autocomplétion + résolution)
    # ------------------------------------------------------------------ #

    async def brands(self, force: bool = False) -> list[dict[str, Any]]:
        """Marques populaires (``/api/v2/brands``) — liste publique fiable."""
        if self._brands is None or force:
            try:
                scraper = await self._scraper_or_start()
                payload = await self._call(scraper._get, "/api/v2/brands")
                self._brands = _extract_brands(payload)
            except Exception as exc:
                log.warning("Impossible de charger les marques populaires : %s", exc)
                self._brands = self._brands or []
        return self._brands

    async def find_brand(self, name: str) -> dict[str, Any] | None:
        """
        Résout un nom en marque connue (``{"id", "title"}``).

        Vinted ne permet plus de chercher une marque par nom sans session
        (initializers et recherche par nom bloqués) : la résolution porte
        donc sur les marques populaires exposées par ``/api/v2/brands``
        (égalité puis correspondance partielle). Pour toute autre marque,
        l'URL de recherche Vinted avec ``brand_ids[]=…`` reste la voie fiable.
        """
        needle = name.strip().lower()
        if not needle:
            return None
        for brand in await self.brands():
            title = str(brand.get("title") or "")
            if title.lower() == needle:
                return {"id": int(brand["id"]), "title": title}
        for brand in await self.brands():
            title = str(brand.get("title") or "")
            if needle in title.lower():
                return {"id": int(brand["id"]), "title": title}
        return None

    async def brand_suggestions(self, current: str, limit: int = 10) -> list[str]:
        """Autocomplétion depuis les marques populaires (préfixe puis contenu)."""
        needle = current.strip().lower()
        if not needle:
            return []
        starts, contains = [], []
        for brand in await self.brands():
            title = str(brand.get("title") or "")
            low = title.lower()
            if low.startswith(needle):
                starts.append(title)
            elif needle in low:
                contains.append(title)
        return (starts + contains)[:limit]
