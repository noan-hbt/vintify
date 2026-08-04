"""Persistance JSON atomique des veilles, articles suivis et statistiques.

Le fichier ``state.json`` vit dans ``DATA_DIR`` (montez un volume Railway
dessus pour que les données survivent aux redéploiements). Chaque écriture
passe par un fichier temporaire + ``os.replace`` : pas de fichier corrompu
en cas d'arrêt brutal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("vintify.storage")

_STORE_VERSION = 1


def new_id(prefix: str) -> str:
    """Identifiant court unique (ex. ``w3f9a2c``)."""
    return f"{prefix}{uuid.uuid4().hex[:6]}"


def _compact(data: dict) -> dict:
    """Retire les valeurs nulles pour garder le JSON lisible."""
    return {k: v for k, v in data.items() if v is not None}


@dataclass
class Watch:
    """Une veille = une recherche Vinted surveillée en continu."""

    id: str
    name: str
    filters: dict                    # SearchFilters.to_dict()
    target_type: str                 # "channel" ou "dm"
    target_id: int
    guild_id: int | None = None
    mention: str | None = None       # "here", "everyone" ou nom de rôle
    interval: int = 60
    enabled: bool = True
    created_at: float = 0.0
    last_run: float | None = None
    last_error: str | None = None
    alerts_sent: int = 0
    seen: list[int] = field(default_factory=list)   # ids d'annonces déjà signalées

    def to_dict(self) -> dict:
        return _compact(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> "Watch":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TrackedItem:
    """Un article précis suivi (baisse de prix, vente, retour)."""

    item_id: int
    url: str
    name: str
    target_type: str
    target_id: int
    guild_id: int | None = None
    mention: str | None = None
    interval: int = 300
    enabled: bool = True
    created_at: float = 0.0
    last_run: float | None = None
    last_error: str | None = None
    last_price: float | None = None
    last_closed: bool | None = None
    last_title: str | None = None
    alerts_sent: int = 0

    def to_dict(self) -> dict:
        return _compact(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> "TrackedItem":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Store:
    """Magasin en mémoire + sauvegarde JSON atomique et asynchrone."""

    def __init__(self, path: Path):
        self.path = path
        self.watches: dict[str, Watch] = {}
        self.tracked: dict[str, TrackedItem] = {}   # clé = str(item_id)
        self.stats: dict[str, int] = {
            "searches": 0,
            "alerts": 0,
            "errors": 0,
            "watches_created": 0,
            "items_tracked": 0,
        }
        self.started_at: float = time.time()
        self._lock = asyncio.Lock()
        self._dirty = False

    # ------------------------------------------------------------------ #
    # Chargement / sauvegarde
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Charge l'état depuis le disque (appelé une fois au démarrage)."""
        if not self.path.exists():
            log.info("Aucun état existant (%s) : démarrage à vide.", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(".corrupt")
            log.error("État illisible (%s) : sauvegarde vers %s et reprise à vide.", exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return
        for watch in raw.get("watches", {}).values():
            try:
                w = Watch.from_dict(watch)
                self.watches[w.id] = w
            except TypeError as exc:
                log.warning("Veille ignorée (données invalides) : %s", exc)
        for item_id, tracked in raw.get("tracked", {}).items():
            try:
                t = TrackedItem.from_dict(tracked)
                self.tracked[str(t.item_id)] = t
            except TypeError as exc:
                log.warning("Article suivi ignoré (données invalides) : %s", exc)
        self.stats.update({k: int(v) for k, v in (raw.get("stats") or {}).items()})
        log.info(
            "État chargé : %d veilles, %d articles suivis.",
            len(self.watches),
            len(self.tracked),
        )

    def mark_dirty(self) -> None:
        self._dirty = True

    async def save(self) -> None:
        """Écrit l'état sur disque si quelque chose a changé.

        Le drapeau est réarmé **avant** l'écriture (sous verrou) : une
        mutation survenue pendant l'écriture le repasse à ``True`` et sera
        donc bien persistée au cycle suivant (pas de perte d'état).
        """
        if not self._dirty:
            return
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            try:
                await asyncio.to_thread(self._write_sync)
            except Exception:
                self._dirty = True     # ré-arme en cas d'échec d'écriture
                raise

    def _write_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "watches": {wid: w.to_dict() for wid, w in self.watches.items()},
            "tracked": {str(t.item_id): t.to_dict() for t in self.tracked.values()},
            "stats": self.stats,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # Veilles
    # ------------------------------------------------------------------ #

    def add_watch(self, watch: Watch) -> None:
        self.watches[watch.id] = watch
        self.stats["watches_created"] += 1
        self.mark_dirty()

    def remove_watch(self, watch_id: str) -> Watch | None:
        watch = self.watches.pop(watch_id, None)
        if watch:
            self.mark_dirty()
        return watch

    def get_watch(self, watch_id: str) -> Watch | None:
        return self.watches.get(watch_id)

    def set_watch_enabled(self, watch_id: str, enabled: bool) -> Watch | None:
        watch = self.watches.get(watch_id)
        if watch and watch.enabled != enabled:
            watch.enabled = enabled
            watch.last_run = None          # re-planifie immédiatement à la reprise
            self.mark_dirty()
        return watch

    def add_seen(self, watch_id: str, item_ids: list[int], limit: int) -> int:
        """Enregistre les ids vus ; renvoie le nombre d'ids réellement nouveaux."""
        watch = self.watches.get(watch_id)
        if not watch or not item_ids:
            return 0
        known = set(watch.seen)
        new = [i for i in item_ids if i not in known]
        if new:
            watch.seen = (watch.seen + new)[-limit:]
            self.mark_dirty()
        return len(new)

    # ------------------------------------------------------------------ #
    # Articles suivis
    # ------------------------------------------------------------------ #

    def add_tracked(self, item: TrackedItem) -> None:
        self.tracked[str(item.item_id)] = item
        self.stats["items_tracked"] += 1
        self.mark_dirty()

    def remove_tracked(self, item_id: int) -> TrackedItem | None:
        item = self.tracked.pop(str(item_id), None)
        if item:
            self.mark_dirty()
        return item

    def get_tracked(self, item_id: int) -> TrackedItem | None:
        return self.tracked.get(str(item_id))

    def set_tracked_enabled(self, item_id: int, enabled: bool) -> TrackedItem | None:
        item = self.tracked.get(str(item_id))
        if item and item.enabled != enabled:
            item.enabled = enabled
            item.last_run = None
            self.mark_dirty()
        return item

    # ------------------------------------------------------------------ #
    # Statistiques
    # ------------------------------------------------------------------ #

    def bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount
        self.mark_dirty()
