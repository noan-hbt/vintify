"""Moteur de surveillance : vérifie les veilles et les articles suivis.

Un seul cœur asynchrone tourne en boucle et traite les cibles arrivées à
échéance, dans l'ordre du plus ancien au plus récent ``last_run`` (aucune
cible ne peut en affamer une autre). Les appels Vinted sont sérialisés par
le service ; les envois Discord sont séquentiels et laissent discord.py
gérer ses propres limites de débit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from vintify_bot.embeds import (
    new_item_alert,
    price_drop_alert,
    reopened_alert,
    sold_alert,
)
from vintify_bot.storage import Store, TrackedItem, Watch

log = logging.getLogger("vintify.poller")


def _num(value: Any) -> float | None:
    """Prix → float : accepte float, str, ou objet ``{"amount": …}``."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Poller:
    def __init__(self, bot: discord.Client, store: Store, vinted, cfg) -> None:
        self.bot = bot
        self.store = store
        self.vinted = vinted
        self.cfg = cfg
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}   # un verrou par veille/suivi

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Verrou par cible : évite qu'un scan initial de commande et le tick
        ne pollent (et n'alertent) la même cible en double."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
            if len(self._locks) > 512:   # purge les verrous des cibles supprimées
                keep = {f"w:{w.id}" for w in self.store.watches.values()}
                keep |= {f"t:{t.item_id}" for t in self.store.tracked.values()}
                self._locks = {k: v for k, v in self._locks.items() if k in keep}
        return lock

    # ------------------------------------------------------------------ #
    # Cycle de vie
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="vintify-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        log.info("Cœur de polling démarré (tick %ds).", self.cfg.poller_tick)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erreur inattendue dans le cœur de polling")
            await asyncio.sleep(self.cfg.poller_tick)

    # ------------------------------------------------------------------ #
    # Cœur de polling
    # ------------------------------------------------------------------ #

    async def tick(self) -> None:
        now = time.time()

        for watch in sorted(
            self.store.watches.values(),
            key=lambda w: (w.last_run or 0, w.id),
        ):
            if not watch.enabled:
                continue
            interval = max(self.cfg.min_interval, watch.interval)
            if watch.last_run is not None and now - watch.last_run < interval:
                continue
            await self._poll_watch(watch)

        for tracked in sorted(
            self.store.tracked.values(),
            key=lambda t: (t.last_run or 0, t.item_id),
        ):
            if not tracked.enabled:
                continue
            interval = max(self.cfg.min_interval, tracked.interval)
            if tracked.last_run is not None and now - tracked.last_run < interval:
                continue
            await self._poll_tracked(tracked)

        await self.store.save()

    # ------------------------------------------------------------------ #
    # Veilles (nouvelles annonces)
    # ------------------------------------------------------------------ #

    async def _poll_watch(self, watch: Watch) -> None:
        async with self._lock_for(f"w:{watch.id}"):
            await self._poll_watch_locked(watch)

    async def _poll_watch_locked(self, watch: Watch) -> None:
        watch.last_run = time.time()
        try:
            items = await self.vinted.search(watch.filters, page_size=self.cfg.page_size)
            self.store.bump("searches")
        except Exception as exc:
            watch.last_error = f"{type(exc).__name__}: {exc}"
            self.store.bump("errors")
            log.warning("Veille %s (%s) en échec : %s", watch.id, watch.name, exc)
            return

        ids: list[int] = []
        by_id: dict[int, dict[str, Any]] = {}
        for item in items:
            try:
                iid = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            ids.append(iid)
            by_id[iid] = item
        known_before = set(watch.seen)
        new_ids = {i for i in ids if i not in known_before}
        self.store.add_seen(watch.id, ids, self.cfg.seen_limit)

        if new_ids:
            # Ordre API conservé (nouveau d'abord) ; les annonces déjà closes
            # (vendues) ne déclenchent pas d'alerte.
            fresh = [
                by_id[iid] for iid in ids
                if iid in new_ids and not by_id[iid].get("is_closed")
            ]
            for item in fresh:
                try:
                    await self._send_embed(
                        new_item_alert(item, watch),
                        target_type=watch.target_type,
                        target_id=watch.target_id,
                        guild_id=watch.guild_id,
                        mention=watch.mention,
                    )
                    watch.alerts_sent += 1
                    self.store.bump("alerts")
                except Exception as exc:
                    log.warning("Envoi d'alerte (veille %s) impossible : %s", watch.id, exc)
                    self.store.bump("errors")
            log.info(
                "Veille %s : %d nouvelle(s) annonce(s) sur %d résultats.",
                watch.id,
                len(fresh),
                len(items),
            )
        watch.last_error = None
        self.store.mark_dirty()   # persiste last_run + effacement de l'erreur

    # ------------------------------------------------------------------ #
    # Articles suivis (prix / disponibilité)
    # ------------------------------------------------------------------ #

    async def _poll_tracked(self, tracked: TrackedItem) -> None:
        async with self._lock_for(f"t:{tracked.item_id}"):
            await self._poll_tracked_locked(tracked)

    async def _poll_tracked_locked(self, tracked: TrackedItem) -> None:
        tracked.last_run = time.time()
        try:
            item = await self.vinted.get_item(tracked.item_id)
        except Exception as exc:
            name = type(exc).__name__
            if name == "VintedNotFoundError":
                # L'annonce n'existe plus : alerte unique puis désactivation.
                try:
                    await self._send_embed(
                        sold_alert({"title": tracked.last_title}, tracked, removed=True),
                        target_type=tracked.target_type,
                        target_id=tracked.target_id,
                        guild_id=tracked.guild_id,
                        mention=tracked.mention,
                    )
                    tracked.alerts_sent += 1
                    self.store.bump("alerts")
                except Exception:
                    log.exception("Envoi impossible pour l'article %s", tracked.item_id)
                tracked.enabled = False
                tracked.last_error = "annonce introuvable (vendue ou retirée)"
                log.info("Article %s introuvable : suivi désactivé.", tracked.item_id)
                self.store.mark_dirty()
                return
            tracked.last_error = f"{name}: {exc}"
            self.store.bump("errors")
            log.warning("Suivi %s en échec : %s", tracked.item_id, exc)
            return

        price = _num(item.get("price"))
        closed = bool(item.get("is_closed"))
        events: list[tuple[str, Any]] = []
        prev = _num(tracked.last_price)

        if price is not None and prev is not None:
            if price < prev - 0.005:
                events.append(("drop", price_drop_alert(item, tracked, prev, price)))
        if closed and not tracked.last_closed:
            events.append(("sold", sold_alert(item, tracked)))
        if not closed and tracked.last_closed:
            events.append(("reopened", reopened_alert(item, tracked)))

        tracked.last_price = price if price is not None else tracked.last_price
        tracked.last_closed = closed
        tracked.last_title = item.get("title") or tracked.last_title
        tracked.last_error = None
        # Persiste last_run/prix/état à chaque cycle réussi : après un
        # redémarrage, on ne re-déclenche pas d'alerte « vendu » en double
        # et le prix de référence est à jour.
        self.store.mark_dirty()

        for kind, embed in events:
            try:
                await self._send_embed(
                    embed,
                    target_type=tracked.target_type,
                    target_id=tracked.target_id,
                    guild_id=tracked.guild_id,
                    mention=tracked.mention,
                )
                tracked.alerts_sent += 1
                self.store.bump("alerts")
                log.info("Article %s : alerte « %s ».", tracked.item_id, kind)
            except Exception as exc:
                log.warning("Envoi d'alerte (suivi %s) impossible : %s", tracked.item_id, exc)
                self.store.bump("errors")

    # ------------------------------------------------------------------ #
    # Envoi Discord
    # ------------------------------------------------------------------ #

    async def _send_embed(
        self,
        embed: discord.Embed,
        *,
        target_type: str,
        target_id: int,
        guild_id: int | None,
        mention: str | None,
    ) -> None:
        content = await self._mention_content(mention, guild_id) if mention else None
        view = discord.ui.View()
        if embed.url:
            view.add_item(
                discord.ui.Button(
                    label="Voir sur Vinted",
                    url=embed.url,
                    style=discord.ButtonStyle.link,
                )
            )

        if target_type == "channel":
            channel = self.bot.get_channel(target_id)
            if channel is None:
                channel = await self.bot.fetch_channel(target_id)
            await channel.send(content=content, embed=embed, view=view)
        else:  # dm
            user = self.bot.get_user(target_id)
            if user is None:
                user = await self.bot.fetch_user(target_id)
            await user.send(content=content, embed=embed, view=view)

    async def _mention_content(self, mention: str, guild_id: int | None) -> str | None:
        """Résout « here », « everyone » ou un nom de rôle en mention Discord."""
        key = mention.strip().lower()
        if key == "here":
            return "@here"
        if key == "everyone":
            return "@everyone"
        if guild_id is not None:
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                role = discord.utils.get(guild.roles, name=mention.strip())
                if role is not None:
                    return role.mention
        log.debug("Mention %r introuvable : ignorée.", mention)
        return None
