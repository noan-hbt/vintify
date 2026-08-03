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
import os
import random
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import parse_qsl, quote, urlparse

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
    "item_id_from_url",
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
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_CSRF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', re.I),
    re.compile(r'["\']CSRF_TOKEN["\']\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'["\']csrfToken["\']\s*:\s*["\']([^"\']+)["\']'),
    # JSON échappé dans le script Next.js : \"CSRF_TOKEN\":\"<uuid>\"
    re.compile(r'\\?"CSRF_TOKEN\\?"\s*:\s*\\?"([^"\\]+)', re.I),
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


def item_id_from_url(url: str) -> int | None:
    """
    Extrait l'id d'article d'une URL Vinted (``…/items/{id}-{slug}``).
    Renvoie ``None`` si ce n'est pas une URL d'article.
    """
    match = re.search(r"/items/(\d+)(?:-[^/?]*)?", url or "")
    return int(match.group(1)) if match else None


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
    cookie_header :
        En-tête ``Cookie`` complet copié depuis le navigateur (DevTools) :
        installe tous les cookies (datadome, cf_clearance, JWT…). Recommandé
        pour les offres/achat, qui exigent aussi ``access_token_web``.
    access_token :
        Valeur du cookie ``access_token_web`` (JWT) : posé en
        ``Authorization: Bearer``. Requis par les endpoints d'offres ;
        automatique si fourni via ``cookie_header``.
    cookie_file :
        Chemin d'un fichier JSON persistant les cookies essentiels
        (session, ``access_token_web``, ``refresh_token_web``, ``anon_id``).
        Chargé au démarrage si aucun cookie n'est fourni, mis à jour à
        chaque ``refresh_token()`` — évite de recopier les cookies à
        chaque expiration du JWT (~2 h). Fichier sensible : ne pas
        committer. Le ``refresh_token_web`` expire ~7 jours.
    hypersolutions_api_key :
        Clé API Hyper Solutions (https://docs.hypersolutions.co) pour
        résoudre les challenges DataDome (endpoints de paiement :
        ``/purchases/checkout/build``, ``checkout/payment``…). Sans elle,
        ces endpoints restent bloqués (403) hors navigateur réel.
        Fallback : variable d'environnement ``HYPERSOLUTIONS_API_KEY``.
        Nécessite le paquet ``curl_cffi`` (TLS imitant Chrome).
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
        cookie_header: str | None = None,
        access_token: str | None = None,
        cookie_file: str | Path | None = None,
        hypersolutions_api_key: str | None = None,
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
        self._cookie_header = cookie_header
        self._access_token = access_token
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.hypersolutions_api_key = (
            hypersolutions_api_key or os.environ.get("HYPERSOLUTIONS_API_KEY")
        )
        self._dd_client: Any = None
        self._browser_datadome: str | None = None
        if (
            not self._cookie_header
            and not self.session_cookie
            and self.cookie_file
            and self.cookie_file.exists()
        ):
            loaded = self._load_cookie_file(self.cookie_file)
            if loaded:
                self._cookie_header = "; ".join(
                    f"{name}={value}" for name, value in loaded.items()
                )
                log.debug("Session chargée depuis %s.", self.cookie_file)
        # cookie datadome du NAVIGATEUR (s'il a été fourni) : requis par le
        # gateway api.vinted.fr, qui refuse le cookie résolu par le solver
        if self._cookie_header:
            for part in self._cookie_header.split(";"):
                key, sep, value = part.strip().partition("=")
                if sep and key == "datadome" and value:
                    self._browser_datadome = value
                    break

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

        if bootstrap:
            self.refresh_session()
        # Ré-applique nos cookies APRÈS le bootstrap : la homepage en pose
        # d'anonymes (anon_id, access_token_web, _vinted_fr_session…) qu'il
        # faut écraser pour conserver la session fournie.
        if self._cookie_header:
            self._install_cookies(self._cookie_header)
        elif self.session_cookie:
            self._install_session_cookie(self.session_cookie)
        self._sync_auth_headers()
        # persiste le fichier de session (les refresh_token() le mettront à jour)
        if self.cookie_file and (self._cookie_header or self.session_cookie):
            self._save_cookie_file()

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
            "Platform": "web",
            "Locale": self.locale,
        }

    def _install_session_cookie(self, value: str) -> None:
        self._clear_cookie(self.session.cookies, self.session_cookie_name)
        self.session.cookies.set(
            self.session_cookie_name, value, domain=f".vinted.{self.domain}", path="/"
        )
        log.debug("Cookie de session installé (%s).", self.session_cookie_name)

    @staticmethod
    def _clear_cookie(cookie_jar: Any, name: str) -> None:
        """Supprime tous les cookies d'un nom donné, quel que soit leur domaine."""
        for cookie in list(cookie_jar):
            if cookie.name == name:
                cookie_jar.clear(cookie.domain, cookie.path, cookie.name)

    @staticmethod
    def _cookie_value(cookie_jar: Any, name: str) -> str | None:
        """Première valeur d'un cookie (robuste aux homonymes multi-domaines)."""
        for cookie in cookie_jar:
            if cookie.name == name and cookie.value:
                return cookie.value
        return None

    def _sync_auth_headers(self) -> None:
        """
        Pose ``Authorization: Bearer <access_token_web>`` si le JWT est
        disponible (cookie fourni via ``cookie_header`` ou paramètre
        ``access_token``). Requis par les endpoints d'offres/achat.
        """
        token = self._access_token or self._cookie_value(
            self.session.cookies, "access_token_web"
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            log.debug("Authorization Bearer posé.")
        else:
            self.session.headers.pop("Authorization", None)

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any] | None:
        """Décode (sans vérifier) les claims d'un JWT — inspecter sub/sid/exp."""
        try:
            import base64 as _b64

            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(_b64.urlsafe_b64decode(payload))
            return claims if isinstance(claims, dict) else None
        except Exception:
            return None

    def refresh_token(self) -> dict[str, Any] | None:
        """
        Rafraîchit les JWT via ``POST /web/api/auth/refresh``
        (``refresh_token_web`` → nouveau ``access_token_web`` + nouveau
        ``refresh_token_web``). Met à jour le header ``Authorization``,
        les cookies et le fichier de session si configuré.

        Renvoie la réponse brute, ou ``None`` si aucun refresh token
        disponible / refusé. Le refresh token tourne à chaque appel
        (l'ancien est invalidé).
        """
        refresh = self._cookie_value(self.session.cookies, "refresh_token_web")
        if not refresh:
            return None
        old_session = self._cookie_value(
            self.session.cookies, self.session_cookie_name
        )
        self._throttle()
        try:
            response = self.session.post(
                self._url("/web/api/auth/refresh"), timeout=self.timeout
            )
        except Exception as exc:
            log.warning("Refresh token : erreur transport (%s).", exc)
            return None
        finally:
            self._last_request_ts = time.monotonic()

        if response.status_code != 200:
            log.warning("Refresh token refusé (HTTP %d).", response.status_code)
            return None
        payload = self._parse_json(response)
        access = (
            payload.get("access_token")
            if isinstance(payload, Mapping) else None
        )
        if not access:
            return None
        # Garde-fou : un refresh qui renvoie des tokens SANS sub (utilisateur)
        # signifie une session anonyme créée par le serveur (refresh token
        # périmé/incohérent). On refuse d'adopter ces tokens.
        access_claims = self._jwt_claims(access)
        if not access_claims or not access_claims.get("sub"):
            log.warning(
                "Refresh renvoyé un access_token anonyme (sub absent) — refusé, "
                "session non remplacée."
            )
            return None

        # remplace l'access_token_web dans le jar
        self._clear_cookie(self.session.cookies, "access_token_web")
        self.session.cookies.set(
            "access_token_web", access, domain=f".vinted.{self.domain}", path="/"
        )
        # le nouveau refresh_token_web arrive en Set-Cookie : normalise son domaine
        new_refresh: str | None = None
        for cookie in self.session.cookies:
            if cookie.name == "refresh_token_web" and cookie.value != refresh:
                new_refresh = cookie.value
        if new_refresh:
            new_claims = self._jwt_claims(new_refresh)
            if not new_claims or not new_claims.get("sub"):
                log.warning("refresh_token_web renvoyé anonyme — non adopté.")
                new_refresh = None
        if new_refresh:
            self._clear_cookie(self.session.cookies, "refresh_token_web")
            self.session.cookies.set(
                "refresh_token_web", new_refresh,
                domain=f".vinted.{self.domain}", path="/",
            )
        # Le refresh peut ROTER la session : il pose alors un NOUVEAU
        # _vinted_fr_session (Set-Cookie) — c'est la seule session cohérente
        # avec les nouveaux JWT. On l'adopte (purge des anciens). Sans
        # rotation, on ré-affirme la session fournie par l'utilisateur.
        new_session: str | None = None
        for cookie in self.session.cookies:
            if cookie.name == self.session_cookie_name and cookie.value != old_session:
                new_session = cookie.value
        if new_session:
            self._clear_cookie(self.session.cookies, self.session_cookie_name)
            self.session.cookies.set(
                self.session_cookie_name, new_session,
                domain=f".vinted.{self.domain}", path="/",
            )
        else:
            if self._cookie_header:
                self._install_cookies(self._cookie_header)
            elif self.session_cookie:
                self._install_session_cookie(self.session_cookie)
        self._sync_auth_headers()
        if self.cookie_file:
            self._save_cookie_file()
        log.info("Tokens rafraîchis (access_token_web + refresh_token_web).")
        return payload

    def _save_cookie_file(self) -> None:
        """Persiste les cookies essentiels (session + JWT) en JSON clair."""
        names = (self.session_cookie_name, "access_token_web", "refresh_token_web", "anon_id")
        data: dict[str, str] = {}
        for name in names:
            # préfère le cookie du domaine attendu (le serveur peut poser des
            # doublons sur .www.vinted.fr qui écraseraient la vraie valeur)
            value = next(
                (
                    c.value
                    for c in self.session.cookies
                    if c.name == name
                    and c.value
                    and c.domain == f".vinted.{self.domain}"
                ),
                None,
            )
            if value is None:
                value = self._cookie_value(self.session.cookies, name)
            if value:
                data[name] = value
        # la session fournie par l'utilisateur prime sur le jar (un refresh
        # peut avoir posé un _vinted_fr_session anonyme par-dessus)
        if self.session_cookie:
            data[self.session_cookie_name] = self.session_cookie
        elif self._cookie_header:
            for part in self._cookie_header.split(";"):
                key, sep, value = part.strip().partition("=")
                if sep and key == self.session_cookie_name and value:
                    data[key] = value
                    break
        # le datadome du navigateur est requis par le gateway api.vinted.fr :
        # on le persiste (la valeur du jar peut être le cookie résolu)
        if self._browser_datadome:
            data["datadome"] = self._browser_datadome
        try:
            self.cookie_file.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            log.debug("Session sauvegardée (%s).", self.cookie_file)
        except OSError as exc:
            log.warning("Impossible d'écrire %s : %s", self.cookie_file, exc)

    @staticmethod
    def _load_cookie_file(path: Path) -> dict[str, str] | None:
        """Charge un fichier de session JSON → dict nom/valeur de cookies."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else None

    def _install_cookies(self, cookie_header: str) -> None:
        """
        Installe tous les cookies d'un en-tête ``Cookie`` brut
        (``"k=v; k2=v2"`` — copier-collable depuis DevTools).
        """
        for part in cookie_header.split(";"):
            name, sep, value = part.strip().partition("=")
            if not sep or not name:
                continue
            self._clear_cookie(self.session.cookies, name)
            self.session.cookies.set(
                name, value, domain=f".vinted.{self.domain}", path="/"
            )
        log.debug("%d paires cookie installées.", cookie_header.count("="))

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

        anon_id = self._cookie_value(self.session.cookies, "anon_id")
        if anon_id:
            self.session.headers["X-Anon-Id"] = anon_id

        # Ré-applique les cookies fournis : la réponse de la homepage peut
        # avoir posé des cookies anonymes/renouvelés par-dessus les nôtres.
        if self._cookie_header:
            self._install_cookies(self._cookie_header)
        elif self.session_cookie:
            self._install_session_cookie(self.session_cookie)
        self._sync_auth_headers()
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
        # itération plutôt que cookies.get() : Vinted peut poser plusieurs
        # cookies homonymes (domaines .vinted.fr / .www.vinted.fr), ce qui
        # ferait lever CookieConflictError à cookies.get().
        return any(
            cookie.name == self.session_cookie_name and bool(cookie.value)
            for cookie in self.session.cookies
        )

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

    # -- résolution DataDome (Hyper Solutions) ---------------------------- #

    def _dd_session(self) -> Any:
        """Client TLS imitant Chrome (curl_cffi) pour les endpoints DataDome."""
        if self._dd_client is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as exc:
                raise VintedError(
                    "curl_cffi requis pour la résolution DataDome "
                    "(pip install curl_cffi)"
                ) from exc
            self._dd_client = cffi_requests.Session(impersonate="chrome")
            self._dd_client.headers.update(self._default_headers())
        return self._dd_client

    def _sync_dd_state(self) -> None:
        """Copie cookies + headers de session vers le client curl_cffi."""
        client = self._dd_session()
        client.cookies.clear()
        for cookie in self.session.cookies:
            client.cookies.set(
                cookie.name, cookie.value, domain=cookie.domain, path=cookie.path
            )
        for name, value in self.session.headers.items():
            if name.lower() not in ("host", "content-length", "content-type"):
                client.headers[name] = value

    @staticmethod
    def _public_ip() -> str:
        """IP publique de la machine (celle qui appelle Vinted)."""
        import requests as _requests

        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                resp = _requests.get(url, timeout=10)
                ip = resp.text.strip()
                if resp.status_code == 200 and ip and not ip.lower().startswith("<"):
                    return ip
            except Exception:
                continue
        return ""

    def _dd_install_cookie(self, client: Any, cookie_line: str) -> bool:
        """Parse ``"datadome=...; Max-Age=...; ..."`` et pose le cookie
        dans le jar de session (cloudscraper), puis re-synchronise le
        client curl_cffi depuis ce jar (source de vérité)."""
        name, _, value = cookie_line.split(";", 1)[0].partition("=")
        if not name or not value:
            return False
        domain = f".vinted.{self.domain}"
        self._clear_cookie(self.session.cookies, name)
        self.session.cookies.set(name, value, domain=domain, path="/")
        client.cookies.clear()
        for cookie in self.session.cookies:
            client.cookies.set(
                cookie.name, cookie.value, domain=cookie.domain, path=cookie.path
            )
        return True

    @staticmethod
    def _parse_dd_block(html: str) -> dict[str, str]:
        """Extrait les champs du ``var dd={...}`` d'un block page DataDome."""
        match = re.search(r"var dd=\{(.*?)\}", html, re.S)
        if not match:
            return {}
        body = match.group(1)

        def field(name: str) -> str:
            mm = re.search(rf"'{name}'\s*:\s*'?([^'}}]*)'?", body)
            return mm.group(1).strip() if mm else ""

        return {n: field(n) for n in ("rt", "cid", "hsh", "t", "s", "e")}

    def _dd_device_link_from_block(
        self, html: str, response: Any, referer: str
    ) -> str | None:
        """
        Construit le deviceLink DataDome depuis un block page HTML (403).
        Le paramètre ``cid`` du lien est la valeur du cookie ``datadome``
        posé par la réponse bloquée (Set-Cookie).
        """
        fields = self._parse_dd_block(html)
        if not fields.get("cid") or not fields.get("rt"):
            return None
        if fields.get("t") == "bv":
            log.warning("DataDome : IP bloquée (t=bv) — résolution inutile.")
            return None
        set_cookie = response.headers.get("Set-Cookie", "")
        dd_cookie = ""
        for part in set_cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "datadome":
                dd_cookie = value
        if not dd_cookie:
            dd_cookie = (
                self._cookie_value(self._dd_session().cookies, "datadome") or ""
            )
        if not dd_cookie:
            log.warning("Cookie datadome absent de la réponse 403.")
            return None
        path = "interstitial" if fields["rt"] == "i" else "captcha"
        return (
            f"https://geo.captcha-delivery.com/{path}/?initialCid={fields['cid']}"
            f"&hash={fields['hsh']}&cid={dd_cookie}&t={fields['t']}"
            f"&referer={quote(referer, safe='')}&s={fields['s']}"
            f"&e={fields['e']}&dm=cd"
        )

    def _solve_datadome(self, device_link: str, parent_url: str | None = None) -> bool:
        """
        Résout un challenge DataDome (interstitial ou slider) via Hyper
        Solutions et pose le cookie ``datadome`` dans le jar de session.
        Route selon le chemin du device link (``/interstitial/`` vs
        ``/captcha/``). Renvoie ``True`` si le cookie est en place.
        """
        if not self.hypersolutions_api_key:
            log.warning("Challenge DataDome sans clé HyperSolutions configurée.")
            return False
        if "/interstitial/" in device_link:
            return self._solve_dd_interstitial(device_link)
        if "/captcha/" in device_link:
            return self._solve_dd_slider(device_link, parent_url)
        log.warning("Challenge DataDome non reconnu : %s", device_link[:120])
        return False

    def _solve_dd_interstitial(self, device_link: str) -> bool:
        """Flow interstitial : POST du payload sur ``geo.captcha-delivery.com``."""
        client = self._dd_session()
        self._sync_dd_state()
        ip = self._public_ip()
        if not ip:
            log.warning("IP publique introuvable — résolution DataDome annulée.")
            return False
        try:
            page = client.get(device_link, timeout=self.timeout)
            if not page.text:
                log.warning("deviceLink vide (HTTP %s).", page.status_code)
                return False

            payload_resp = client.post(
                "https://datadome.hypersolutions.co/interstitial",
                headers={"x-api-key": self.hypersolutions_api_key},
                json={
                    "userAgent": self.user_agent,
                    "deviceLink": device_link,
                    "html": page.text,
                    "ip": ip,
                    "acceptLanguage": self.session.headers.get(
                        "Accept-Language", f"{self.locale},en;q=0.8"
                    ),
                },
                timeout=self.timeout,
            )
            if payload_resp.status_code != 200:
                log.warning(
                    "HyperSolutions interstitial → HTTP %s : %s",
                    payload_resp.status_code,
                    payload_resp.text[:200],
                )
                return False
            data = payload_resp.json()
            payload = (data or {}).get("payload") if isinstance(data, dict) else None
            if not payload:
                log.warning("Payload HyperSolutions vide.")
                return False

            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            extra = data.get("headers") if isinstance(data, dict) else None
            if isinstance(extra, dict):
                headers.update(extra)
            check = client.post(
                "https://geo.captcha-delivery.com/interstitial/",
                data=payload,
                headers=headers,
                timeout=self.timeout,
            )
            result: Any = {}
            try:
                result = check.json()
            except Exception:
                result = {}
            if not isinstance(result, dict):
                result = {}
            if result.get("view") == "captcha":
                log.warning("DataDome demande un slider captcha — non géré.")
                return False
            cookie_line = result.get("cookie")
            if not cookie_line:
                log.warning("Pas de cookie dans la réponse DataDome : %s", check.text[:200])
                return False
            if not self._dd_install_cookie(client, cookie_line):
                return False
            log.info("Challenge DataDome interstitial résolu : cookie %s posé.",
                     cookie_line.split(";", 1)[0].partition("=")[0])
            return True
        except Exception as exc:  # réseau / JSON inattendu
            log.warning("Échec de la résolution DataDome : %s", exc)
            return False

    def _solve_dd_slider(self, device_link: str, parent_url: str | None) -> bool:
        """Flow slider : puzzle + piece extraits du deviceLink, check URL."""
        client = self._dd_session()
        self._sync_dd_state()
        ip = self._public_ip()
        if not ip:
            log.warning("IP publique introuvable — résolution DataDome annulée.")
            return False
        try:
            client.headers["Referer"] = parent_url or self.base_url
            page = client.get(device_link, timeout=self.timeout)
            html = page.text or ""
            if not html:
                log.warning("deviceLink vide (HTTP %s).", page.status_code)
                return False
            match = re.search(r"captchaChallengePath:\s*['\"]([^'\"]+\.jpg)['\"]", html)
            if not match:
                log.warning("captchaChallengePath introuvable dans le deviceLink.")
                return False
            puzzle_url = match.group(1)
            piece_url = puzzle_url[:-4] + ".frag.png"
            client.headers["Referer"] = device_link
            puzzle = client.get(puzzle_url, timeout=self.timeout)
            piece = client.get(piece_url, timeout=self.timeout)
            import base64

            puzzle_b64 = base64.b64encode(puzzle.content).decode()
            piece_b64 = base64.b64encode(piece.content).decode()

            resp = client.post(
                "https://datadome.hypersolutions.co/slider",
                headers={"x-api-key": self.hypersolutions_api_key},
                json={
                    "userAgent": self.user_agent,
                    "deviceLink": device_link,
                    "html": html,
                    "puzzle": puzzle_b64,
                    "piece": piece_b64,
                    "parentUrl": parent_url or device_link,
                    "ip": ip,
                    "acceptLanguage": self.session.headers.get(
                        "Accept-Language", f"{self.locale},en;q=0.8"
                    ),
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                log.warning(
                    "HyperSolutions slider → HTTP %s : %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            data = resp.json()
            check_url = (data or {}).get("payload") if isinstance(data, dict) else None
            if not check_url:
                log.warning("Check URL slider vide.")
                return False
            check = client.get(check_url, timeout=self.timeout)
            result: Any = {}
            try:
                result = check.json()
            except Exception:
                result = {}
            if not isinstance(result, dict):
                result = {}
            cookie_line = result.get("cookie")
            if not cookie_line:
                log.warning("Pas de cookie dans la réponse slider : %s", check.text[:200])
                return False
            if not self._dd_install_cookie(client, cookie_line):
                return False
            log.info("Challenge DataDome slider résolu : cookie %s posé.",
                     cookie_line.split(";", 1)[0].partition("=")[0])
            return True
        except Exception as exc:  # réseau / JSON inattendu
            log.warning("Échec de la résolution DataDome : %s", exc)
            return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        retries: int | None = None,
        require_auth: bool = False,
        datadome: bool = False,
        browser_datadome: bool = False,
    ) -> Any:
        """
        Requête JSON (GET/POST) avec retry exponentiel.

        Erreurs traduites : 429 → ``VintedRateLimitError``,
        401/403 → ``VintedAuthError`` (après tentative de refresh CSRF),
        404 → ``VintedNotFoundError``, reste → ``VintedError``.

        ``datadome=True`` route la requête via le client TLS Chrome
        (curl_cffi) — requis pour les endpoints protégés par DataDome
        (``/purchases/…``). Si un challenge interstitial est renvoyé
        (403 + body JSON ``{"url": "...captcha-delivery.com/..."}``), il est
        résolu via Hyper Solutions puis retenté une seule fois.
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
        dd_attempts = 0
        last_error: VintedError | None = None

        for attempt in range(attempts + 1):
            self._throttle()
            log.debug(
                "%s %s params=%s body=%s (essai %d/%d)",
                method, url, query, json_body, attempt + 1, attempts + 1,
            )
            try:
                if datadome or dd_attempts:
                    self._sync_dd_state()
                    if browser_datadome and self._browser_datadome:
                        # gateway api.vinted.fr : seul le cookie datadome du
                        # navigateur est accepté (le résolu est refusé) ; le
                        # Referer doit être une page de checkout ; et le
                        # header Authorization (Bearer) y est REFUSÉ (403
                        # access_denied) — le gateway s'authentifie par
                        # cookies uniquement
                        self._dd_session().cookies.set(
                            "datadome", self._browser_datadome,
                            domain=f".vinted.{self.domain}", path="/",
                        )
                        self._dd_session().headers["Referer"] = (
                            f"{self.base_url}/checkout/"
                        )
                        self._dd_session().headers.pop("Authorization", None)
                    response = self._dd_session().request(
                        method, url, params=query, json=json_body, timeout=self.timeout
                    )
                else:
                    response = self.session.request(
                        method, url, params=query, json=json_body, timeout=self.timeout
                    )
            except Exception as exc:
                last_error = VintedError(f"Erreur transport : {exc}", url=url)
                if attempt >= attempts:
                    break
                time.sleep(self._backoff(attempt))
                continue
            finally:
                self._last_request_ts = time.monotonic()

            status = response.status_code

            if status in (200, 201):
                return self._parse_json(response)

            # Challenge DataDome inline : 403 + JSON {"url": "...captcha-delivery.com/..."}
            # ou block page HTML (``var dd={...}`` + ct.captcha-delivery.com).
            # (jusqu'à 2 résolutions : DataDome re-challenge parfois une 2e fois)
            dd_device_link: str | None = None
            if status == 403:
                text = response.text or ""
                try:
                    body = response.json()
                    if (
                        isinstance(body, dict)
                        and isinstance(body.get("url"), str)
                        and "captcha-delivery.com" in body["url"]
                    ):
                        dd_device_link = body["url"]
                except Exception:
                    body = None
                if (
                    dd_device_link is None
                    and "var dd={" in text
                    and "captcha-delivery.com" in text
                ):
                    dd_device_link = self._dd_device_link_from_block(text, response, url)
            if dd_device_link and dd_attempts < 2 and attempt < attempts:
                dd_attempts += 1
                log.info("403 DataDome sur %s → résolution du challenge…", path)
                if self._solve_datadome(dd_device_link, url):
                    time.sleep(self._backoff(attempt))
                    continue
                raise VintedAuthError(
                    "Bloqué par DataDome : résolution impossible "
                    "(clé HyperSolutions invalide, IP bloquée ou slider captcha).",
                    status=status,
                    url=url,
                    body=response.text[:300],
                )

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

            login_required = status == 400 and "user_login_required" in (response.text or "")
            if status in (401, 403) or login_required:
                if self.auto_refresh_csrf and not refreshed and attempt < attempts:
                    refreshed = True
                    log.info(
                        "%s sur %s → rafraîchissement des tokens de session.",
                        status, path,
                    )
                    try:
                        # 1) refresh JWT (refresh_token_web) ; 2) sinon
                        # re-bootstrap de la session (CSRF + cookies).
                        if not self.refresh_token():
                            self.refresh_session()
                    except VintedError as exc:
                        raise VintedAuthError(str(exc), status=status, url=url) from exc
                    time.sleep(self._backoff(attempt))
                    continue
                if login_required:
                    raise VintedAuthError(
                        "Session non authentifiée (token expiré ?).",
                        status=status,
                        url=url,
                        body=response.text[:300],
                    )
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

    def _get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        retries: int | None = None,
        require_auth: bool = False,
        datadome: bool = False,
    ) -> Any:
        """GET JSON — mêmes règles d'erreur que ``_request``."""
        return self._request(
            "GET",
            path,
            params=params,
            retries=retries,
            require_auth=require_auth,
            datadome=datadome,
        )

    def _post(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        retries: int | None = None,
        require_auth: bool = False,
        datadome: bool = False,
    ) -> Any:
        """POST JSON — mêmes règles d'erreur que ``_request``."""
        return self._request(
            "POST",
            path,
            json_body=json_body,
            retries=retries,
            require_auth=require_auth,
            datadome=datadome,
        )

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
    # Offres (cookie de session requis)
    # ------------------------------------------------------------------ #

    def get_offer_request_options(
        self,
        item_ids: Sequence[int | str],
        seller_id: int | str,
        item_price: float | int | str,
        currency: str = "EUR",
    ) -> dict[str, Any]:
        """
        Contraintes d'offre pour un ou plusieurs articles
        (``POST /api/v2/offers/request_options``).

        Ne crée rien : renvoie le dict complet de la réponse
        (``request_options`` : min/max price, remise max, nombre
        d'offres restantes, suggestions…).
        """
        payload = self._post(
            "/api/v2/offers/request_options",
            json_body={
                "price": {
                    "amount": str(item_price),
                    "currency_code": currency,
                },
                "item_ids": [int(item_id) for item_id in item_ids],
                "seller_id": int(seller_id),
            },
            require_auth=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def make_offer(
        self,
        transaction_id: int | str,
        price: float | int | str,
        currency: str = "EUR",
    ) -> dict[str, Any]:
        """
        Envoie une offre sur une transaction
        (``POST /api/v2/transactions/{id}/offer_requests``).

        Succès : ``{"offer_request": {id, transaction_id, status: 10,
        price}, "code": 0}`` — renvoie le dict complet.
        """
        payload = self._post(
            f"/api/v2/transactions/{transaction_id}/offer_requests",
            json_body={
                "offer_request": {
                    "price": str(price),
                    "currency": currency,
                },
            },
            require_auth=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def create_transaction(
        self,
        item_ids: Sequence[int | str],
        seller_id: int | str,
        buyer_id: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Crée la conversation + transaction d'achat pour un article
        (``POST /api/v2/conversations``, initiator
        ``buyer_enters_offer_form``). Étape préalable à ``make_offer`` :
        renvoie le dict de la transaction, dont l'``id`` sert ensuite
        d'``transaction_id``.

        Effet de bord : ouvre un fil de conversation avec le vendeur
        (comme si l'acheteur cliquait « Faire une offre » sur la page
        article). ``buyer_id`` est ignoré (déduit de la session).
        """
        payload = self._post(
            "/api/v2/conversations",
            json_body={
                "initiator": "buyer_enters_offer_form",
                "item_id": int(item_ids[0]),
                "opposite_user_id": int(seller_id),
            },
            require_auth=True,
        )
        if isinstance(payload, Mapping):
            conversation = payload.get("conversation", payload)
            if isinstance(conversation, Mapping):
                transaction = conversation.get("transaction")
                if isinstance(transaction, Mapping):
                    return transaction
                return conversation
            return payload
        return {}

    # ------------------------------------------------------------------ #
    # Achat (checkout — couche paiement protégée DataDome/Incognia)
    # ------------------------------------------------------------------ #

    def get_credit_cards(self) -> list[dict[str, Any]]:
        """Cartes enregistrées (``GET /payments/credit_cards``)."""
        payload = self._get("/api/v2/payments/credit_cards", require_auth=True)
        if isinstance(payload, Mapping):
            for key in ("cards", "credit_cards"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [c for c in value if isinstance(c, dict)]
        return []

    def build_checkout(
        self,
        purchase_item_id: int | str,
        purchase_type: str = "item",
    ) -> dict[str, Any]:
        """
        Prépare le checkout (``POST /purchases/checkout/build``) — gratuit,
        ne débite rien. ``purchase_item_id`` = transaction ou item selon
        ``purchase_type`` (``"item"`` / ``"transaction"`` / ``"offer_request"``).
        Protégé par DataDome/Incognia : peut répondre 403 sans navigateur.
        """
        payload = self._post(
            "/api/v2/purchases/checkout/build",
            json_body={
                "purchase_items": [{"id": int(purchase_item_id), "type": purchase_type}],
            },
            require_auth=True,
            datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def get_checkout(self, checkout_id: int | str) -> dict[str, Any]:
        """État du checkout (``GET /purchases/{id}/checkout``)."""
        payload = self._get(f"/api/v2/purchases/{checkout_id}/checkout", require_auth=True, datadome=True)
        return payload if isinstance(payload, Mapping) else {}

    def update_checkout(
        self,
        checkout_id: int | str,
        components: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Met à jour les composants du checkout
        (``PUT /purchases/{id}/checkout``) : adresse de livraison, transport,
        assurance… Ex. : ``{"components": {"additional_service":
        {"is_selected": false}}}`` pour désactiver l'assurance.
        """
        payload = self._request(
            "PUT",
            f"/api/v2/purchases/{checkout_id}/checkout",
            json_body={"components": dict(components or {})},
            require_auth=True,
            datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def get_nearby_pickup_points(
        self,
        checkout_id: int | str,
        latitude: float,
        longitude: float,
        *,
        shipping_order_id: int | str | None = None,
        country_code: str = "FR",
    ) -> dict[str, Any]:
        """
        Points relais disponibles pour un checkout
        (``GET api.vinted.fr/shipping-estimation/external/shipping_orders/
        {id}/nearby_pickup_points`` — passerelle API, hors ``/api/v2``).

        Renvoie le dict brut : ``shipping_points`` (chaque élément contient
        ``point`` : code, uuid, adresse…), ``shipping_rates`` (tarifs par
        transporteur) et ``suggested_shipping_point_code``.

        ``shipping_order_id`` est extrait automatiquement du checkout s'il
        n'est pas fourni.
        """
        if shipping_order_id is None:
            co = self.get_checkout(checkout_id).get("checkout", {})
            comps = co.get("components", {})
            shipping_order_id = (
                comps.get("shipping_address", {}) or {}
            ).get("shipping_order_id")
            if shipping_order_id is None:
                raise VintedError(
                    "shipping_order_id introuvable dans le checkout "
                    f"{checkout_id} — fournis-le explicitement."
                )
        client = self._dd_session()
        self._sync_dd_state()
        client.headers["Referer"] = f"{self.base_url}/checkout/"
        url = (
            f"https://api.vinted.{self.domain}/shipping-estimation/external/"
            f"shipping_orders/{shipping_order_id}/nearby_pickup_points"
        )
        payload = self._request(
            "GET",
            url,
            params={
                "country_code": country_code,
                "latitude": latitude,
                "longitude": longitude,
            },
            require_auth=True,
            datadome=True,
            browser_datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def select_pickup_point(
        self,
        checkout_id: int | str,
        *,
        rate_uuid: str,
        point_code: int | str,
        point_uuid: str,
    ) -> dict[str, Any]:
        """
        Sélectionne un point relais sur le checkout
        (``PUT /purchases/{id}/checkout`` — composant
        ``shipping_pickup_details`` : ``rate_uuid`` + ``point_code`` +
        ``point_uuid``). Renvoie le checkout mis à jour.
        """
        return self.update_checkout(
            checkout_id,
            components={
                "shipping_pickup_details": {
                    "rate_uuid": rate_uuid,
                    "point_code": str(point_code),
                    "point_uuid": point_uuid,
                }
            },
        )

    def get_checkout_payment(self, checkout_id: int | str) -> dict[str, Any]:
        """
        État du paiement (``GET /purchases/{id}/checkout/payment``) :
        checksum + moyens de paiement disponibles. Protégé DataDome.
        """
        payload = self._get(
            f"/api/v2/purchases/{checkout_id}/checkout/payment",
            require_auth=True,
            datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def pay_checkout(
        self,
        checkout_id: int | str,
        *,
        checksum: str,
        card_id: int | str,
        security_code: str | None = None,
        payment_options: Mapping[str, Any] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """
        Déclenche le paiement (``POST /purchases/{id}/checkout/payment``) —
        💶 DÉBITE RÉELLEMENT la carte. Garde-fou : ``confirm=True`` requis,
        sinon ``RuntimeError``. À n'utiliser qu'en phase de test assumée.
        """
        if not confirm:
            raise RuntimeError(
                "pay_checkout débite réellement la carte : passer "
                "confirm=True pour confirmer (aucune requête envoyée)."
            )
        body: dict[str, Any] = {
            "checksum": checksum,
            "payment_options": dict(payment_options or {"card_id": int(card_id)}),
        }
        if security_code:
            body["card_details"] = {"security_code": security_code}
        payload = self._post(
            f"/api/v2/purchases/{checkout_id}/checkout/payment",
            json_body=body,
            require_auth=True,
            datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

    def continue_checkout_payment(
        self,
        checkout_id: int | str,
        *,
        cvv: str | None = None,
        checksum: str | None = None,
        blik_code: str | None = None,
        secure_3ds_details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Poursuit un paiement (``POST /purchases/{id}/checkout/payment/continue``) :
         CVV, code BLIK ou résultat 3DS renvoyé par ``pay_checkout``.
        """
        body: dict[str, Any] = {}
        if cvv:
            body["cvv"] = cvv
        if checksum:
            body["checksum"] = checksum
        if blik_code:
            body["blik_code"] = blik_code
        if secure_3ds_details:
            body["secure_3ds_details"] = dict(secure_3ds_details)
        payload = self._post(
            f"/api/v2/purchases/{checkout_id}/checkout/payment/continue",
            json_body=body,
            require_auth=True,
            datadome=True,
        )
        return payload if isinstance(payload, Mapping) else {}

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
