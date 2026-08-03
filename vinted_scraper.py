"""
vinted_scraper
~~~~~~~~~~~~~~

Client HTTP synchrone pour l'API interne (non documentée) de Vinted.

Points clés
-----------
* Contournement Cloudflare via ``cloudscraper`` (fingerprint Chrome/Windows).
* Bootstrap de session : récupération des cookies anonymes + extraction du
  token CSRF depuis la homepage, injecté dans ``X-CSRF-Token``.
* Couche HTTP unique (``_get``) avec backoff exponentiel, gestion 429/403/404,
  rafraîchissement automatique du CSRF et validation du Content-Type.
* Pagination générique (``_paginate``) avec arrêt automatique et tqdm optionnel.
* Support optionnel du cookie de session pour les endpoints authentifiés
  (messagerie privée).

Usage minimal
-------------
    from vinted_scraper import VintedScraper, Sort

    with VintedScraper(domain="fr") as vinted:
        for item in vinted.search_items("nike tn", price_to=60, order=Sort.NEWEST):
            print(item["id"], item["title"])

Avertissement : l'API ciblée est privée et peut changer sans préavis.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import parse_qsl, urlparse

import cloudscraper

try:  # dépendance optionnelle
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover
    _tqdm = None


__version__ = "1.0.0"

__all__ = [
    "VintedScraper",
    "SearchFilters",
    "Sort",
    "VintedError",
    "VintedAuthError",
    "VintedNotFoundError",
    "VintedRateLimitError",
    "VintedResponseError",
    "normalize_item",
    "normalize_user",
    "KNOWN_DOMAINS",
]


# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

KNOWN_DOMAINS: dict[str, str] = {
    "fr": "fr-FR",
    "be": "fr-BE",
    "nl": "nl-NL",
    "de": "de-DE",
    "at": "de-AT",
    "es": "es-ES",
    "it": "it-IT",
    "pt": "pt-PT",
    "pl": "pl-PL",
    "cz": "cs-CZ",
    "sk": "sk-SK",
    "lt": "lt-LT",
    "lv": "lv-LV",
    "se": "sv-SE",
    "fi": "fi-FI",
    "dk": "da-DK",
    "ro": "ro-RO",
    "hu": "hu-HU",
    "gr": "el-GR",
    "lu": "fr-LU",
    "ie": "en-IE",
    "co.uk": "en-GB",
    "com": "en-US",
}

DEFAULT_DOMAIN = "fr"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 5
DEFAULT_PAGE_SIZE = 96
MAX_PAGE_SIZE = 96

BASE_DELAY = 1.0          # base du backoff exponentiel : BASE_DELAY * 2 ** attempt
MAX_DELAY = 60.0
PAGE_DELAY = 0.3          # pause entre deux pages
MIN_REQUEST_INTERVAL = 0.25  # throttle global entre deux requêtes

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CSRF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', re.I),
    re.compile(r'["\']CSRF_TOKEN["\']\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'["\']csrfToken["\']\s*:\s*["\']([^"\']+)["\']'),
)

# Clés susceptibles de contenir la liste utile dans une réponse paginée.
_LIST_KEYS: tuple[str, ...] = (
    "items",
    "users",
    "user_feedbacks",
    "feedbacks",
    "followers",
    "following",
    "msg_threads",
    "threads",
    "catalogs",
    "brands",
    "results",
    "data",
)

log = logging.getLogger("vinted_scraper")
log.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class VintedError(Exception):
    """Erreur générique du client Vinted."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - cosmétique
        parts = [self.message]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.url:
            parts.append(f"→ {self.url}")
        return " ".join(parts)


class VintedAuthError(VintedError):
    """403/401 : token CSRF expiré, cookie invalide ou endpoint authentifié."""


class VintedNotFoundError(VintedError):
    """404 : ressource inexistante ou supprimée."""


class VintedRateLimitError(VintedError):
    """429 : quota dépassé, retries épuisés."""


class VintedResponseError(VintedError):
    """Réponse non-JSON (page Cloudflare, captcha, maintenance…)."""


# --------------------------------------------------------------------------- #
# Tri
# --------------------------------------------------------------------------- #

class Sort(str, Enum):
    """Valeurs acceptées par le paramètre ``order`` du catalogue."""

    NEWEST = "newest_first"
    RELEVANCE = "relevance"
    PRICE_ASC = "price_low_to_high"
    PRICE_DESC = "price_high_to_low"

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_csv(value: Any) -> str | None:
    """Normalise un scalaire ou un itérable en chaîne CSV (``"1,2,3"``)."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        return text.strip() or None
    if isinstance(value, Iterable):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return ",".join(parts) or None
    return str(value)


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Supprime les valeurs nulles / vides et sérialise les booléens."""
    if not params:
        return {}
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, Enum):
            out[key] = value.value
        else:
            out[key] = value
    return out


def _price_to_float(value: Any) -> float | None:
    """Gère les deux formats de prix renvoyés par l'API (float ou objet)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    if isinstance(value, Mapping):
        return _price_to_float(value.get("amount"))
    return None


def _currency_of(value: Any, fallback: str | None = None) -> str | None:
    if isinstance(value, Mapping):
        return value.get("currency_code") or value.get("currency") or fallback
    return fallback


def _extract_list(payload: Any, key: str | None = None) -> list[dict[str, Any]]:
    """Extrait la liste utile d'une réponse paginée."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    if key:
        value = payload.get(key)
        return list(value) if isinstance(value, list) else []
    for candidate in _LIST_KEYS:
        value = payload.get(candidate)
        if isinstance(value, list):
            return value
    return []


def normalize_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Aplatit une annonce en dictionnaire stable, pratique pour un embed Discord."""
    photo = raw.get("photo") or {}
    photos = raw.get("photos") or []
    if not photo and photos:
        photo = photos[0] or {}
    user = raw.get("user") or {}
    price = raw.get("price")
    total = raw.get("total_item_price") or raw.get("service_fee")

    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "brand": raw.get("brand_title") or (raw.get("brand") or {}).get("title"),
        "brand_id": raw.get("brand_id"),
        "size": raw.get("size_title") or raw.get("size"),
        "status": raw.get("status"),
        "price": _price_to_float(price),
        "total_price": _price_to_float(total),
        "currency": _currency_of(price, raw.get("currency")),
        "url": raw.get("url"),
        "photo": photo.get("full_size_url") or photo.get("url"),
        "photo_count": len(photos) or (1 if photo else 0),
        "favourites": raw.get("favourite_count"),
        "views": raw.get("view_count"),
        "user_id": raw.get("user_id") or user.get("id"),
        "user_login": user.get("login"),
        "country": raw.get("country_code") or user.get("country_code"),
        "created_at": raw.get("created_at_ts") or raw.get("created_at"),
        "updated_at": raw.get("updated_at_ts") or raw.get("updated_at"),
        "is_closed": bool(raw.get("is_closed")),
        "promoted": bool(raw.get("promoted")),
    }


def normalize_user(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Version à plat d'un profil vendeur (cf. ``get_user_stats``)."""
    photo = raw.get("photo") or {}
    return {
        "id": raw.get("id"),
        "login": raw.get("login"),
        "real_name": raw.get("real_name"),
        "city": raw.get("city"),
        "country": raw.get("country_title_local") or raw.get("country_code"),
        "created_at": raw.get("created_at"),
        "last_logged_on": raw.get("last_loged_on_ts") or raw.get("last_logged_on_ts"),
        "item_count": raw.get("item_count"),
        "given_item_count": raw.get("given_item_count"),
        "taken_item_count": raw.get("taken_item_count"),
        "followers": raw.get("followers_count"),
        "following": raw.get("following_count"),
        "positive_feedback": raw.get("positive_feedback_count"),
        "negative_feedback": raw.get("negative_feedback_count"),
        "neutral_feedback": raw.get("neutral_feedback_count"),
        "feedback_count": raw.get("feedback_count"),
        "feedback_reputation": raw.get("feedback_reputation"),
        "is_verified": bool((raw.get("verification") or {}).get("email", {}).get("valid")),
        "is_business": bool(raw.get("business")),
        "avatar": photo.get("full_size_url") or photo.get("url"),
        "profile_url": raw.get("profile_url"),
    }


# --------------------------------------------------------------------------- #
# Filtres de recherche (sérialisables → monitors.json)
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class SearchFilters:
    """
    Jeu de filtres réutilisable et sérialisable (JSON) pour les moniteurs.

        f = SearchFilters.from_url("https://www.vinted.fr/catalog?search_text=tn&price_to=60")
        items = vinted.search_items(filters=f, pages=2)
    """

    search_text: str | None = None
    catalog_ids: Sequence[int | str] | None = None
    brand_ids: Sequence[int | str] | None = None
    size_ids: Sequence[int | str] | None = None
    status_ids: Sequence[int | str] | None = None
    color_ids: Sequence[int | str] | None = None
    material_ids: Sequence[int | str] | None = None
    country_ids: Sequence[int | str] | None = None
    city_ids: Sequence[int | str] | None = None
    price_from: float | None = None
    price_to: float | None = None
    currency: str | None = None
    is_for_swap: bool | None = None
    order: str = Sort.NEWEST.value
    extra: dict[str, Any] = field(default_factory=dict)

    # -- (dé)sérialisation ------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {})}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchFilters":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("extra", {})
        payload["extra"].update({k: v for k, v in data.items() if k not in known})
        return cls(**payload)

    @classmethod
    def from_url(cls, url: str) -> "SearchFilters":
        """Convertit une URL de recherche Vinted en filtres exploitables."""
        query = parse_qsl(urlparse(url).query, keep_blank_values=False)
        multi: dict[str, list[str]] = {}
        for key, value in query:
            multi.setdefault(key.removesuffix("[]"), []).append(value)

        def ids(name: str) -> list[int] | None:
            raw = multi.get(name) or multi.get(name.rstrip("s"))
            if not raw:
                return None
            out: list[int] = []
            for chunk in raw:
                for part in chunk.split(","):
                    part = part.strip()
                    if part.isdigit():
                        out.append(int(part))
            return out or None

        def one(name: str) -> str | None:
            values = multi.get(name)
            return values[0] if values else None

        price_from = one("price_from")
        price_to = one("price_to")
        return cls(
            search_text=one("search_text"),
            catalog_ids=ids("catalog_ids") or ids("catalog"),
            brand_ids=ids("brand_ids") or ids("brand"),
            size_ids=ids("size_ids") or ids("size"),
            status_ids=ids("status_ids") or ids("status"),
            color_ids=ids("color_ids") or ids("color"),
            material_ids=ids("material_ids") or ids("material"),
            country_ids=ids("country_ids"),
            city_ids=ids("city_ids"),
            price_from=float(price_from) if price_from else None,
            price_to=float(price_to) if price_to else None,
            currency=one("currency"),
            order=one("order") or Sort.NEWEST.value,
        )

    # -- conversion API ---------------------------------------------------- #

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "search_text": self.search_text,
            "catalog_ids": _as_csv(self.catalog_ids),
            "brand_ids": _as_csv(self.brand_ids),
            "size_ids": _as_csv(self.size_ids),
            "status_ids": _as_csv(self.status_ids),
            "color_ids": _as_csv(self.color_ids),
            "material_ids": _as_csv(self.material_ids),
            "country_ids": _as_csv(self.country_ids),
            "city_ids": _as_csv(self.city_ids),
            "price_from": self.price_from,
            "price_to": self.price_to,
            "currency": self.currency,
            "is_for_swap": self.is_for_swap,
            "order": self.order,
        }
        params.update(self.extra or {})
        return _clean_params(params)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class VintedScraper:
    """
    Client synchrone de l'API interne Vinted.

    Paramètres
    ----------
    domain :
        Suffixe du domaine (``fr``, ``nl``, ``com``, ``pl``, ``it``, ``es``,
        ``de``, ``co.uk``…). Détermine l'URL de base et l'``Accept-Language``.
    session_cookie :
        Valeur du cookie ``_vinted_<x>_session``, requise pour les endpoints
        authentifiés (messagerie).
    timeout, retries :
        Comportement réseau ; ``retries`` s'applique aux 429/5xx et erreurs
        transport.
    verbose :
        Active les logs DEBUG sur le logger ``vinted_scraper``.
    """

    # -- cycle de vie ------------------------------------------------------ #

    def __init__(
        self,
        domain: str = DEFAULT_DOMAIN,
        *,
        session_cookie: str | None = None,
        session_cookie_name: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        verbose: bool = False,
        user_agent: str = USER_AGENT,
        proxies: Mapping[str, str] | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL,
        auto_refresh_csrf: bool = True,
        bootstrap: bool = True,
    ) -> None:
        self.domain = domain.lstrip(".").lower()
        if self.domain not in KNOWN_DOMAINS:
            log.warning("Domaine %r inconnu : tentative quand même.", self.domain)
        self.base_url = f"https://www.vinted.{self.domain}"
        self.locale = KNOWN_DOMAINS.get(self.domain, "en-US")

        self.timeout = timeout
        self.retries = max(0, retries)
        self.verbose = verbose
        self.user_agent = user_agent
        self.min_request_interval = max(0.0, min_request_interval)
        self.auto_refresh_csrf = auto_refresh_csrf

        self.csrf_token: str | None = None
        self.session_cookie = session_cookie
        self.session_cookie_name = session_cookie_name or self._default_cookie_name()

        self._last_request_ts = 0.0
        self._initializers_cache: dict[str, Any] | None = None

        if verbose:
            log.setLevel(logging.DEBUG)
            if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
                )
                log.addHandler(handler)

        self.session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )
        if proxies:
            self.session.proxies.update(dict(proxies))
        self.session.headers.update(self._default_headers())

        if self.session_cookie:
            self._install_session_cookie(self.session_cookie)
        if bootstrap:
            self.refresh_session()

    def __enter__(self) -> "VintedScraper":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass

    def __repr__(self) -> str:  # pragma: no cover - cosmétique
        auth = "auth" if self.session_cookie else "anon"
        return f"<VintedScraper domain={self.domain!r} {auth} csrf={bool(self.csrf_token)}>"

    # -- session / anti-bot ------------------------------------------------ #

    def _default_cookie_name(self) -> str:
        token = "fr" if self.domain == "com" else self.domain.replace(".", "_")
        return f"_vinted_{token}_session"

    def _default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self.locale},{self.locale.split('-')[0]};q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"{self.base_url}/",
            "Origin": self.base_url,
            "Connection": "keep-alive",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "X-Requested-With": "XMLHttpRequest",
        }

    def _install_session_cookie(self, value: str) -> None:
        self.session.cookies.set(
            self.session_cookie_name, value, domain=f".vinted.{self.domain}", path="/"
        )
        log.debug("Cookie de session installé (%s).", self.session_cookie_name)

    def refresh_session(self) -> str | None:
        """
        (Re)initialise la session : GET homepage → cookies anonymes + CSRF.
        Retourne le token trouvé (``None`` si absent, ce qui reste jouable
        pour les endpoints publics).
        """
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            response = self.session.get(
                f"{self.base_url}/", headers=headers, timeout=self.timeout
            )
        except Exception as exc:
            raise VintedError(f"Impossible de joindre {self.base_url} : {exc}") from exc

        if response.status_code >= 400:
            raise VintedError(
                "Bootstrap de session refusé (Cloudflare ?)",
                status=response.status_code,
                url=response.url,
                body=response.text[:400],
            )

        self.csrf_token = self._extract_csrf(response.text)
        if self.csrf_token:
            self.session.headers["X-CSRF-Token"] = self.csrf_token
            log.debug("CSRF token acquis (%s…).", self.csrf_token[:12])
        else:
            self.session.headers.pop("X-CSRF-Token", None)
            log.warning("Aucun token CSRF trouvé sur la homepage.")

        anon_id = self.session.cookies.get("anon_id")
        if anon_id:
            self.session.headers["X-Anon-Id"] = anon_id
        return self.csrf_token

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        for pattern in _CSRF_PATTERNS:
            match = pattern.search(html)
            if match:
                return match.group(1)
        return None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.session.cookies.get(self.session_cookie_name))

    # -- couche HTTP ------------------------------------------------------- #

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _throttle(self) -> None:
        if self.min_request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        wait = self.min_request_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _backoff(attempt: int) -> float:
        delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
        return delay * random.uniform(0.85, 1.25)  # jitter

    def _get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        retries: int | None = None,
        require_auth: bool = False,
    ) -> Any:
        """
        GET JSON avec retry exponentiel.

        Erreurs traduites : 429 → ``VintedRateLimitError``,
        401/403 → ``VintedAuthError`` (après tentative de refresh CSRF),
        404 → ``VintedNotFoundError``, reste → ``VintedError``.
        """
        if require_auth and not self.is_authenticated:
            raise VintedAuthError(
                f"{path} nécessite un cookie de session "
                f"({self.session_cookie_name})."
            )

        attempts = self.retries if retries is None else max(0, retries)
        url = self._url(path)
        query = _clean_params(params)
        refreshed = False
        last_error: VintedError | None = None

        for attempt in range(attempts + 1):
            self._throttle()
            log.debug("GET %s params=%s (essai %d/%d)", url, query, attempt + 1, attempts + 1)
            try:
                response = self.session.get(url, params=query, timeout=self.timeout)
            except Exception as exc:
                last_error = VintedError(f"Erreur transport : {exc}", url=url)
                if attempt >= attempts:
                    break
                time.sleep(self._backoff(attempt))
                continue
            finally:
                self._last_request_ts = time.monotonic()

            status = response.status_code

            if status == 200:
                return self._parse_json(response)

            if status == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else self._backoff(attempt)
                except ValueError:
                    delay = self._backoff(attempt)
                last_error = VintedRateLimitError(
                    "Rate limit atteint", status=status, url=url
                )
                if attempt >= attempts:
                    break
                log.warning("429 sur %s → pause %.1fs", path, delay)
                time.sleep(min(delay, MAX_DELAY))
                continue

            if status in (401, 403):
                if self.auto_refresh_csrf and not refreshed and attempt < attempts:
                    refreshed = True
                    log.info("%s sur %s → rafraîchissement de la session.", status, path)
                    try:
                        self.refresh_session()
                    except VintedError as exc:
                        raise VintedAuthError(str(exc), status=status, url=url) from exc
                    time.sleep(self._backoff(attempt))
                    continue
                raise VintedAuthError(
                    "Accès refusé : CSRF expiré, cookie invalide ou IP bloquée.",
                    status=status,
                    url=url,
                    body=response.text[:300],
                )

            if status == 404:
                raise VintedNotFoundError("Ressource introuvable", status=status, url=url)

            if 500 <= status < 600:
                last_error = VintedError("Erreur serveur Vinted", status=status, url=url)
                if attempt >= attempts:
                    break
                time.sleep(self._backoff(attempt))
                continue

            raise VintedError(
                "Réponse inattendue", status=status, url=url, body=response.text[:300]
            )

        raise last_error or VintedError("Échec de la requête", url=url)

    @staticmethod
    def _parse_json(response: Any) -> Any:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" not in content_type:
            raise VintedResponseError(
                f"Réponse non-JSON (Content-Type={content_type or 'inconnu'})",
                status=response.status_code,
                url=response.url,
                body=response.text[:300],
            )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise VintedResponseError(
                f"JSON illisible : {exc}",
                status=response.status_code,
                url=response.url,
                body=response.text[:300],
            ) from exc

    # -- pagination -------------------------------------------------------- #

    def _paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        pages: int | None = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        key: str | None = None,
        progress: bool = False,
        delay: float = PAGE_DELAY,
        dedupe: bool = True,
        require_auth: bool = False,
        start_page: int = 1,
        max_pages: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """
        Générateur paginé (``page`` / ``per_page``).

        ``pages=None`` ou ``0`` → parcourt jusqu'à épuisement (borné par
        ``max_pages``). Arrêt anticipé si une page est vide ou incomplète.
        """
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        total_pages = max_pages if not pages else min(pages, max_pages)
        base = dict(params or {})
        seen_ids: set[Any] = set()

        bar = None
        if progress and _tqdm is not None:
            bar = _tqdm(total=total_pages if pages else None, unit="page", desc=path)

        try:
            for offset in range(total_pages):
                page = start_page + offset
                payload = self._get(
                    path,
                    {**base, "page": page, "per_page": page_size},
                    require_auth=require_auth,
                )
                batch = _extract_list(payload, key)
                if bar is not None:
                    bar.update(1)
                if not batch:
                    log.debug("Page %d vide → arrêt de la pagination.", page)
                    break

                for entry in batch:
                    if dedupe and isinstance(entry, Mapping):
                        entry_id = entry.get("id")
                        if entry_id is not None:
                            if entry_id in seen_ids:
                                continue
                            seen_ids.add(entry_id)
                    yield entry

                if len(batch) < page_size:
                    log.debug("Dernière page atteinte (%d < %d).", len(batch), page_size)
                    break
                if offset + 1 < total_pages and delay > 0:
                    time.sleep(delay)
        finally:
            if bar is not None:
                bar.close()

    # ------------------------------------------------------------------ #
    # Catalogue / recherche
    # ------------------------------------------------------------------ #

    def iter_items(
        self,
        search_text: str | None = None,
        *,
        filters: SearchFilters | Mapping[str, Any] | None = None,
        catalog_ids: Sequence[int | str] | None = None,
        brand_ids: Sequence[int | str] | None = None,
        size_ids: Sequence[int | str] | None = None,
        status_ids: Sequence[int | str] | None = None,
        color_ids: Sequence[int | str] | None = None,
        material_ids: Sequence[int | str] | None = None,
        country_ids: Sequence[int | str] | None = None,
        city_ids: Sequence[int | str] | None = None,
        price_from: float | None = None,
        price_to: float | None = None,
        currency: str | None = None,
        is_for_swap: bool | None = None,
        order: str | Sort = Sort.NEWEST,
        pages: int | None = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress: bool = False,
        normalize: bool = False,
        **extra: Any,
    ) -> Iterator[dict[str, Any]]:
        """Version paresseuse de :meth:`search_items` (idéal pour le monitoring)."""
        if isinstance(filters, Mapping):
            filters = SearchFilters.from_dict(filters)
        params = filters.to_params() if filters else {}

        overrides = _clean_params(
            {
                "search_text": search_text,
                "catalog_ids": _as_csv(catalog_ids),
                "brand_ids": _as_csv(brand_ids),
                "size_ids": _as_csv(size_ids),
                "status_ids": _as_csv(status_ids),
                "color_ids": _as_csv(color_ids),
                "material_ids": _as_csv(material_ids),
                "country_ids": _as_csv(country_ids),
                "city_ids": _as_csv(city_ids),
                "price_from": price_from,
                "price_to": price_to,
                "currency": currency,
                "is_for_swap": is_for_swap,
                "order": order,
            }
        )
        params.update(overrides)
        params.update(_clean_params(extra))
        params.setdefault("order", str(Sort.NEWEST))

        stream = self._paginate(
            "/api/v2/catalog/items",
            params,
            pages=pages,
            page_size=page_size,
            key="items",
            progress=progress,
        )
        for raw in stream:
            yield normalize_item(raw) if normalize else raw

    def search_items(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """
        Recherche multi-filtres paginée → liste d'annonces.

            vinted.search_items("veste carhartt", brand_ids=[362],
                                price_to=80, pages=3, order=Sort.NEWEST)
        """
        return list(self.iter_items(*args, **kwargs))

    def get_item(self, item_id: int | str, *, normalize: bool = False) -> dict[str, Any]:
        """Détail d'un article (``/api/v2/items/{id}``)."""
        payload = self._get(f"/api/v2/items/{item_id}")
        item = payload.get("item", payload) if isinstance(payload, Mapping) else {}
        return normalize_item(item) if normalize else item

    def get_item_full(self, item_id: int | str) -> dict[str, Any]:
        """
        Détail étendu (``/api/v2/items/{id}/full``) : articles similaires,
        remises de lot, données vendeur… Retombe sur ``get_item`` si absent.
        """
        try:
            payload = self._get(f"/api/v2/items/{item_id}/full")
        except VintedNotFoundError:
            return self.get_item(item_id)
        if isinstance(payload, Mapping):
            return payload.get("item", payload)
        return {}

    def get_similar_items(
        self,
        item_id: int | str,
        *,
        limit: int = 20,
        normalize: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Articles similaires (``/api/v2/items/{id}/recommendations``).
        Base de calcul de la sous-cotation côté bot.
        """
        try:
            payload = self._get(
                f"/api/v2/items/{item_id}/recommendations", {"per_page": limit}
            )
            items = _extract_list(payload, "items")
        except (VintedNotFoundError, VintedResponseError):
            items = []

        if not items:  # fallback : bloc "similar_items" du /full
            full = self.get_item_full(item_id)
            items = _extract_list(full, "similar_items") or _extract_list(
                full, "related_items"
            )

        items = items[:limit]
        return [normalize_item(i) for i in items] if normalize else items

    def get_item_price_history(self, item_id: int | str) -> list[dict[str, Any]]:
        """
        Historique de prix, extrait du payload ``/full`` quand Vinted l'expose.
        Retourne ``[]`` si l'information est absente.
        """
        full = self.get_item_full(item_id)
        for key in ("price_history", "price_changes", "discount_history"):
            history = full.get(key)
            if isinstance(history, list):
                return history
        current = _price_to_float(full.get("price"))
        if current is None:
            return []
        return [
            {
                "price": current,
                "currency": _currency_of(full.get("price")),
                "timestamp": full.get("updated_at_ts") or full.get("updated_at"),
                "source": "current",
            }
        ]

    def item_url(self, item_id: int | str, slug: str = "") -> str:
        suffix = f"-{slug}" if slug else ""
        return f"{self.base_url}/items/{item_id}{suffix}"

    # ------------------------------------------------------------------ #
    # Utilisateurs
    # ------------------------------------------------------------------ #

    def get_user(self, user_id: int | str) -> dict[str, Any]:
        """Profil brut (``/api/v2/users/{id}``)."""
        payload = self._get(f"/api/v2/users/{user_id}")
        return payload.get("user", payload) if isinstance(payload, Mapping) else {}

    def get_user_stats(self, user_id: int | str) -> dict[str, Any]:
        """Profil normalisé à plat (compteurs, réputation, ancienneté)."""
        return normalize_user(self.get_user(user_id))

    def iter_user_items(
        self,
        user_id: int | str,
        *,
        pages: int | None = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        order: str | Sort = Sort.NEWEST,
        progress: bool = False,
        normalize: bool = False,
    ) -> Iterator[dict[str, Any]]:
        stream = self._paginate(
            f"/api/v2/users/{user_id}/items",
            {"order": order},
            pages=pages,
            page_size=page_size,
            key="items",
            progress=progress,
        )
        for raw in stream:
            yield normalize_item(raw) if normalize else raw

    def get_user_items(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Annonces d'un vendeur (``/api/v2/users/{id}/items``)."""
        return list(self.iter_user_items(*args, **kwargs))

    def iter_user_feedback(
        self,
        user_id: int | str,
        *,
        pages: int | None = 1,
        page_size: int = 20,
        by: str = "all",
        progress: bool = False,
    ) -> Iterator[dict[str, Any]]:
        return self._paginate(
            "/api/v2/user_feedbacks",
            {"user_id": user_id, "by": by},
            pages=pages,
            page_size=page_size,
            key="user_feedbacks",
            progress=progress,
        )

    def get_user_feedback(
        self,
        user_id: int | str,
        *,
        pages: int | None = 1,
        page_size: int = 20,
        by: str = "all",
        progress: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Avis reçus. Tente l'endpoint moderne ``/api/v2/user_feedbacks`` puis
        retombe sur ``/api/v2/users/{id}/feedbacks``.
        """
        try:
            feedback = list(
                self.iter_user_feedback(
                    user_id, pages=pages, page_size=page_size, by=by, progress=progress
                )
            )
            if feedback:
                return feedback
        except (VintedNotFoundError, VintedResponseError):
            pass
        return list(
            self._paginate(
                f"/api/v2/users/{user_id}/feedbacks",
                pages=pages,
                page_size=page_size,
                progress=progress,
            )
        )

    def get_user_followers(
        self, user_id: int | str, *, pages: int | None = 1, page_size: int = 50
    ) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                f"/api/v2/users/{user_id}/followers",
                pages=pages,
                page_size=page_size,
                key="users",
            )
        )

    def get_user_following(
        self, user_id: int | str, *, pages: int | None = 1, page_size: int = 50
    ) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                f"/api/v2/users/{user_id}/following",
                pages=pages,
                page_size=page_size,
                key="users",
            )
        )

    def search_users(
        self,
        query: str,
        *,
        pages: int | None = 1,
        page_size: int = 20,
        normalize: bool = False,
    ) -> list[dict[str, Any]]:
        """Recherche de pseudo (``/api/v2/users/search``)."""
        stream = self._paginate(
            "/api/v2/users/search",
            {"search_text": query},
            pages=pages,
            page_size=page_size,
            key="users",
        )
        return [normalize_user(u) if normalize else u for u in stream]

    # ------------------------------------------------------------------ #
    # Marques & données de référence
    # ------------------------------------------------------------------ #

    def get_brand_details(self, brand_id: int | str) -> dict[str, Any]:
        """Infos marque (``/api/v2/brands/{id}``)."""
        payload = self._get(f"/api/v2/brands/{brand_id}")
        return payload.get("brand", payload) if isinstance(payload, Mapping) else {}

    def get_catalog_initializers(self, *, refresh: bool = False) -> dict[str, Any]:
        """
        Données de référence (catégories, marques, tailles, couleurs, matériaux).
        Essaie ``/api/v2/catalog/initializers`` puis ``/api/v2/catalogs`` puis
        ``/api/v1/catalogs``. Résultat mis en cache en mémoire.
        """
        if self._initializers_cache is not None and not refresh:
            return self._initializers_cache

        endpoints = ("/api/v2/catalog/initializers", "/api/v2/catalogs", "/api/v1/catalogs")
        last_error: VintedError | None = None
        for endpoint in endpoints:
            try:
                payload = self._get(endpoint)
            except VintedError as exc:
                last_error = exc
                log.debug("Initializers KO sur %s : %s", endpoint, exc)
                continue
            if isinstance(payload, Mapping) and payload:
                self._initializers_cache = dict(payload)
                return self._initializers_cache
        raise last_error or VintedError("Aucun endpoint d'initializers disponible")

    def _dtos(self, refresh: bool = False) -> dict[str, Any]:
        payload = self.get_catalog_initializers(refresh=refresh)
        dtos = payload.get("dtos")
        return dtos if isinstance(dtos, Mapping) else payload

    def get_categories(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return _extract_list(self._dtos(refresh), "catalogs")

    def get_brands(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return _extract_list(self._dtos(refresh), "brands")

    def get_sizes(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        dtos = self._dtos(refresh)
        return _extract_list(dtos, "size_groups") or _extract_list(dtos, "sizes")

    def get_colors(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return _extract_list(self._dtos(refresh), "colors")

    def get_materials(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return _extract_list(self._dtos(refresh), "materials")

    def get_statuses(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return _extract_list(self._dtos(refresh), "statuses")

    def find_brand(self, name: str) -> dict[str, Any] | None:
        """Résolution nom → marque (insensible à la casse) via les initializers."""
        needle = name.strip().casefold()
        for brand in self.get_brands():
            if str(brand.get("title", "")).casefold() == needle:
                return brand
        for brand in self.get_brands():
            if needle in str(brand.get("title", "")).casefold():
                return brand
        return None

    # ------------------------------------------------------------------ #
    # Messagerie privée (cookie de session requis)
    # ------------------------------------------------------------------ #

    def get_messages(
        self,
        user_id: int | str,
        *,
        pages: int | None = 1,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Fils de discussion d'un compte (``/api/v2/users/{id}/msg_threads``)."""
        return list(
            self._paginate(
                f"/api/v2/users/{user_id}/msg_threads",
                pages=pages,
                page_size=page_size,
                key="msg_threads",
                require_auth=True,
            )
        )

    def get_thread(self, thread_id: int | str) -> dict[str, Any]:
        """Contenu d'un fil (``/api/v2/msg_threads/{id}``)."""
        payload = self._get(f"/api/v2/msg_threads/{thread_id}", require_auth=True)
        return payload.get("thread", payload) if isinstance(payload, Mapping) else {}

    def get_messages_with_user(
        self,
        me_id: int | str,
        other_user_id: int | str,
        *,
        pages: int | None = 5,
    ) -> dict[str, Any]:
        """
        Retrouve le fil ouvert avec ``other_user_id`` puis en renvoie le détail.
        ``{}`` si aucun fil ne correspond.
        """
        target = str(other_user_id)
        for thread in self.get_messages(me_id, pages=pages):
            opposite = thread.get("opposite_user") or {}
            candidates = {
                str(opposite.get("id")),
                str(thread.get("opposite_user_id")),
                *(str(u.get("id")) for u in (thread.get("users") or [])),
            }
            if target in candidates:
                thread_id = thread.get("id")
                return self.get_thread(thread_id) if thread_id else thread
        return {}

    # ------------------------------------------------------------------ #
    # Médias
    # ------------------------------------------------------------------ #

    def download_item_images(
        self,
        item_id: int | str,
        *,
        directory: str | Path = "images",
        overwrite: bool = False,
        progress: bool = False,
    ) -> list[Path]:
        """
        Télécharge toutes les photos d'un article dans
        ``<directory>/<item_id>/`` et renvoie les chemins écrits.
        """
        item = self.get_item_full(item_id)
        photos = item.get("photos") or []
        if not photos and item.get("photo"):
            photos = [item["photo"]]

        urls: list[str] = []
        for photo in photos:
            if not isinstance(photo, Mapping):
                continue
            url = photo.get("full_size_url") or photo.get("url")
            if url:
                urls.append(url)

        target = Path(directory) / str(item_id)
        target.mkdir(parents=True, exist_ok=True)

        iterator: Iterable[tuple[int, str]] = enumerate(urls, start=1)
        if progress and _tqdm is not None:
            iterator = _tqdm(list(iterator), unit="img", desc=f"item {item_id}")

        written: list[Path] = []
        for index, url in iterator:
            suffix = Path(urlparse(url).path).suffix or ".jpg"
            path = target / f"{index:02d}{suffix}"
            if path.exists() and not overwrite:
                written.append(path)
                continue
            try:
                self._throttle()
                response = self.session.get(url, timeout=self.timeout, stream=True)
                self._last_request_ts = time.monotonic()
                response.raise_for_status()
                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            handle.write(chunk)
                written.append(path)
            except Exception as exc:
                log.warning("Téléchargement échoué (%s) : %s", url, exc)
        return written


# --------------------------------------------------------------------------- #
# Démo CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Recherche rapide sur Vinted.")
    parser.add_argument("query", nargs="?", default="nike", help="texte recherché")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--price-to", type=float, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    with VintedScraper(domain=args.domain, verbose=args.verbose) as vinted:
        results = vinted.search_items(
            args.query,
            price_to=args.price_to,
            pages=args.pages,
            progress=True,
            normalize=True,
        )
        print(f"{len(results)} annonces")
        for item in results[:20]:
            print(
                f"{item['id']:>12}  {str(item['price']):>7} {item['currency'] or ''}"
                f"  {(item['brand'] or '—')[:18]:<18}  {item['title'][:45]}"
            )
