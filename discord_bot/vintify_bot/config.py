"""Configuration du bot, chargée depuis les variables d'environnement.

Toutes les valeurs ont un défaut raisonnable : seul ``DISCORD_TOKEN`` est
obligatoire pour démarrer le bot (les autres sont utiles sur Railway).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Lit un entier d'environnement, borné, avec valeur par défaut."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


@dataclass(frozen=True)
class Config:
    """Configuration immutable du bot."""

    token: str
    domain: str = "fr"                      # domaine Vinted (fr, be, de, es, it…)
    data_dir: Path = Path("data")           # persistance (volume Railway → /data)
    default_interval: int = 60              # s entre deux vérifications d'une veille
    suivi_interval: int = 300               # s entre deux vérifications d'un article suivi
    min_interval: int = 30                  # borne basse de l'intervalle
    max_interval: int = 3600                # borne haute de l'intervalle
    page_size: int = 24                     # annonces lues par cycle de veille (max 96)
    seen_limit: int = 1500                  # historique d'ids vus conservé par veille
    max_display: int = 5                    # résultats max d'une /recherche
    poller_tick: int = 10                   # cadence du cœur de polling (s)
    log_level: str = "INFO"
    startup_scan: bool = True               # première vérification immédiate d'une veille

    @classmethod
    def from_env(cls) -> "Config":
        raw_scan = os.environ.get("VEILLE_SCAN_INITIAL", "1").strip().lower()
        return cls(
            token=os.environ.get("DISCORD_TOKEN", "").strip(),
            domain=(os.environ.get("VINTED_DOMAIN", "fr").strip().lower() or "fr"),
            data_dir=Path(os.environ.get("DATA_DIR", "data").strip() or "data"),
            default_interval=_env_int("VEILLE_INTERVALLE_DEFAUT", 60, lo=30, hi=3600),
            suivi_interval=_env_int("SUIVI_INTERVALLE_DEFAUT", 300, lo=30, hi=3600),
            min_interval=_env_int("VEILLE_INTERVALLE_MIN", 30, lo=10, hi=3600),
            max_interval=_env_int("VEILLE_INTERVALLE_MAX", 3600, lo=60, hi=86_400),
            page_size=_env_int("VEILLE_PAGE_SIZE", 24, lo=5, hi=96),
            seen_limit=_env_int("VEILLE_HISTORIQUE_MAX", 1500, lo=50, hi=10_000),
            max_display=_env_int("RECHERCHE_MAX_AFFICHEE", 5, lo=1, hi=10),
            poller_tick=_env_int("POLLER_TICK", 10, lo=2, hi=60),
            log_level=(os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
            startup_scan=raw_scan not in ("0", "false", "no", "off"),
        )
