"""Commandes slash du bot (français).

Groupes :
- ``/veille``      — surveiller une recherche (ajouter / liste / retirer / pause / reprendre)
- ``/suivi``       — suivre un article précis (ajouter / retirer)
- ``/recherche``   — recherche ponctuelle
- ``/stats``       — statistiques du bot
- ``/aide``        — aide générale
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from vintify_bot.embeds import COLOR_VINTED, item_embed
from vintify_bot.poller import Poller, _num
from vintify_bot.storage import Store, TrackedItem, Watch, new_id

log = logging.getLogger("vintify.cogs")

_INTERVAL_CHOICES = [
    app_commands.Choice(name="30 s", value=30),
    app_commands.Choice(name="1 min", value=60),
    app_commands.Choice(name="2 min", value=120),
    app_commands.Choice(name="5 min", value=300),
    app_commands.Choice(name="15 min", value=900),
    app_commands.Choice(name="30 min", value=1800),
    app_commands.Choice(name="1 h", value=3600),
]


def _filters_summary(filters: dict[str, Any]) -> str:
    """Résumé lisible des filtres d'une veille."""
    parts: list[str] = []
    if filters.get("search_text"):
        parts.append(f"« {filters['search_text']} »")
    if filters.get("brand_ids"):
        parts.append(f"marque #{filters['brand_ids']}")
    if filters.get("price_from") is not None:
        parts.append(f"≥ {filters['price_from']:g} {filters.get('currency') or ''}".strip())
    if filters.get("price_to") is not None:
        parts.append(f"≤ {filters['price_to']:g} {filters.get('currency') or ''}".strip())
    if filters.get("catalog_ids"):
        parts.append(f"catégorie #{filters['catalog_ids']}")
    if filters.get("country_ids"):
        parts.append(f"pays #{filters['country_ids']}")
    return " · ".join(parts) or "tous les articles"


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="❌ Erreur", description=message[:2000], color=0xE74C3C)


class VintifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: Store, vinted, poller: Poller, cfg) -> None:
        self.bot = bot
        self.store = store
        self.vinted = vinted
        self.poller = poller
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    # Préchargement (autocomplétion des marques)
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        asyncio.create_task(self._warm_brands(), name="vintify-brands")

    async def _warm_brands(self) -> None:
        try:
            await self.vinted.brands()
            log.info("Référentiel des marques chargé (%d entrées).", len(self.vinted._brands or []))
        except Exception as exc:
            log.debug("Préchargement des marques impossible : %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _resolve_filters(
        self,
        query: str,
        price_min: float | None,
        price_max: float | None,
        brand: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Construit les filtres depuis un texte ou une URL de recherche Vinted.

        Retourne ``(filters, erreur)`` — un seul des deux est non nul.
        """
        from vinted_scraper import SearchFilters, item_id_from_url

        text = query.strip()
        if not text:
            return None, "La requête est vide."

        if item_id_from_url(text):
            return None, "C'est une URL d'article : utilisez **/suivi ajouter** pour suivre ce type de lien."

        if text.lower().startswith(("http://", "https://")):
            try:
                filters = SearchFilters.from_url(text)
            except Exception as exc:
                return None, f"Impossible de lire l'URL de recherche : {exc}"
            if not any(
                filters.to_dict()
            ):
                return None, "L'URL ne contient aucun filtre de recherche exploitable."
        else:
            filters = SearchFilters(search_text=text)

        if brand:
            found = await self.vinted.find_brand(brand)
            if found is None:
                popular = await self.vinted.brands()
                names = ", ".join(str(b.get("title")) for b in popular[:8])
                return None, (
                    f"Marque « {brand} » introuvable dans la liste publique "
                    "des marques.\n"
                    "**Astuce** : sur vinted.fr, appliquez le filtre marque, "
                    "puis collez l'URL obtenue (elle contient `brand_ids[]=…`) "
                    "dans cette commande — tous les filtres seront repris.\n"
                    f"Marques reconnues : {names}…"
                )
            filters.brand_ids = [int(found["id"])]

        if price_min is not None:
            filters.price_from = price_min
        if price_max is not None:
            filters.price_to = price_max
        return filters.to_dict(), None

    def _target(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        """Cible d'envoi des alertes : salon choisi, salon courant, ou MP."""
        if channel is not None:
            return "channel", channel.id, channel.guild.id if channel.guild else None
        if interaction.guild is not None:
            return "channel", interaction.channel_id, interaction.guild.id
        return "dm", interaction.user.id, None

    async def _watch_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        out = []
        for watch in self.store.watches.values():
            if watch.id.startswith(current.lower()):
                out.append(app_commands.Choice(name=f"{watch.id} · {watch.name}", value=watch.id))
        return out[:20]

    async def _tracked_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        out = []
        for tracked in self.store.tracked.values():
            label = f"#{tracked.item_id} · {tracked.name}"
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:90], value=str(tracked.item_id)))
        return out[:20]

    # ------------------------------------------------------------------ #
    # /veille
    # ------------------------------------------------------------------ #

    veille = app_commands.Group(name="veille", description="Surveiller une recherche Vinted en continu.")

    @veille.command(name="ajouter", description="Crée une veille : nouvelles annonces signalées automatiquement.")
    @app_commands.describe(
        requete="Texte de recherche ou URL de recherche Vinted (ex. https://www.vinted.fr/catalog?search_text=tn)",
        nom="Nom de la veille (affiché dans les alertes)",
        prix_min="Prix minimum",
        prix_max="Prix maximum",
        marque="Filtrer par marque (autocomplétion)",
        intervalle="Fréquence de vérification",
        canal="Salon où envoyer les alertes (défaut : salon actuel, sinon MP)",
        mention='Mentionner "@here", "everyone" ou un nom de rôle',
    )
    async def veille_ajouter(
        self,
        interaction: discord.Interaction,
        requete: str,
        nom: str | None = None,
        prix_min: float | None = None,
        prix_max: float | None = None,
        marque: str | None = None,
        intervalle: app_commands.Choice[int] | None = None,
        canal: discord.TextChannel | None = None,
        mention: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        filters, error = await self._resolve_filters(requete, prix_min, prix_max, marque)
        if error:
            await interaction.followup.send(embed=_error_embed(error), ephemeral=True)
            return

        target_type, target_id, guild_id = self._target(interaction, canal)
        watch = Watch(
            id=new_id("w"),
            name=(nom or requete).strip()[:80],
            filters=filters,
            target_type=target_type,
            target_id=target_id,
            guild_id=guild_id,
            mention=(mention or "").strip()[:40] or None,
            interval=intervalle.value if intervalle else self.cfg.default_interval,
            created_at=time.time(),
        )
        self.store.add_watch(watch)

        summary = discord.Embed(
            title="✅ Veille créée",
            description=(
                f"**{watch.name}**\n"
                f"Filtres : {_filters_summary(filters)}\n"
                f"Fréquence : {watch.interval}s · Cible : "
                f"{'MP' if target_type == 'dm' else f'<#{target_id}>'}"
                + (f" · Mention : {watch.mention}" if watch.mention else "")
            ),
            color=COLOR_VINTED,
        )
        summary.set_footer(text=f"ID de la veille : {watch.id} — /veille liste pour gérer")
        await interaction.followup.send(embed=summary, ephemeral=True)
        await self.store.save()

        # Première vérification immédiate : les annonces déjà présentes sont
        # signalées une fois, puis seules les NOUVELLES le seront.
        if self.cfg.startup_scan:
            try:
                await self.poller._poll_watch(watch)
                await self.store.save()
            except Exception as exc:
                log.warning("Scan initial de la veille %s en échec : %s", watch.id, exc)
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="⚠️ Scan initial en échec",
                        description=(
                            "La veille est créée mais la première vérification a échoué "
                            f"(`{exc}`). Le prochain cycle réessaiera automatiquement."
                        ),
                        color=0xF1C40F,
                    ),
                    ephemeral=True,
                )

    @veille.command(name="liste", description="Liste les veilles et les articles suivis.")
    async def veille_liste(self, interaction: discord.Interaction) -> None:
        lines: list[str] = []
        for watch in sorted(self.store.watches.values(), key=lambda w: w.created_at):
            state = "⏸" if not watch.enabled else ("⚠" if watch.last_error else "✅")
            target = "MP" if watch.target_type == "dm" else f"<#{watch.target_id}>"
            lines.append(
                f"{state} **{watch.id}** · {watch.name} · {watch.interval}s · {target}"
                f" · {watch.alerts_sent} 🔔"
            )
            if watch.last_error:
                lines.append(f"   └ ⚠ {watch.last_error[:120]}")
        for tracked in sorted(self.store.tracked.values(), key=lambda t: t.created_at):
            state = "⏸" if not tracked.enabled else ("⚠" if tracked.last_error else "✅")
            target = "MP" if tracked.target_type == "dm" else f"<#{tracked.target_id}>"
            price = f"{tracked.last_price:g} €" if tracked.last_price is not None else "—"
            lines.append(
                f"{state} #{tracked.item_id} · {tracked.name[:40]} · {price} · "
                f"{tracked.interval}s · {target} · {tracked.alerts_sent} 🔔"
            )
            if tracked.last_error:
                lines.append(f"   └ ⚠ {tracked.last_error[:120]}")

        if not lines:
            description = "Aucune veille ni article suivi.\n\n" \
                          "• **/veille ajouter** pour surveiller une recherche\n" \
                          "• **/suivi ajouter** pour suivre un article précis"
        else:
            description = "\n".join(lines)
            if len(description) > 3900:
                description = description[:3900] + "\n… (tronqué)"

        embed = discord.Embed(
            title=f"📋 Veilles et suivis ({len(self.store.watches)} + {len(self.store.tracked)})",
            description=description,
            color=COLOR_VINTED,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @veille.command(name="retirer", description="Supprime une veille.")
    @app_commands.describe(id="Identifiant de la veille (/veille liste)")
    @app_commands.autocomplete(id=_watch_choices)
    async def veille_retirer(self, interaction: discord.Interaction, id: str) -> None:
        watch = self.store.remove_watch(id.strip().lower())
        if watch is None:
            await interaction.response.send_message(embed=_error_embed(f"Veille `{id}` introuvable."), ephemeral=True)
            return
        await self.store.save()
        embed = discord.Embed(
            title="🗑️ Veille supprimée",
            description=f"**{watch.name}** (`{watch.id}`) — {watch.alerts_sent} alertes envoyées.",
            color=COLOR_VINTED,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @veille.command(name="pause", description="Met une veille en pause (sans la supprimer).")
    @app_commands.describe(id="Identifiant de la veille (/veille liste)")
    @app_commands.autocomplete(id=_watch_choices)
    async def veille_pause(self, interaction: discord.Interaction, id: str) -> None:
        watch = self.store.set_watch_enabled(id.strip().lower(), False)
        if watch is None:
            await interaction.response.send_message(embed=_error_embed(f"Veille `{id}` introuvable."), ephemeral=True)
            return
        await self.store.save()
        await interaction.response.send_message(
            embed=discord.Embed(title="⏸ Veille en pause", description=f"**{watch.name}** (`{watch.id}`).", color=COLOR_VINTED),
            ephemeral=True,
        )

    @veille.command(name="reprendre", description="Reprend une veille en pause.")
    @app_commands.describe(id="Identifiant de la veille (/veille liste)")
    @app_commands.autocomplete(id=_watch_choices)
    async def veille_reprendre(self, interaction: discord.Interaction, id: str) -> None:
        watch = self.store.set_watch_enabled(id.strip().lower(), True)
        if watch is None:
            await interaction.response.send_message(embed=_error_embed(f"Veille `{id}` introuvable."), ephemeral=True)
            return
        await self.store.save()
        await interaction.response.send_message(
            embed=discord.Embed(title="▶️ Veille reprise", description=f"**{watch.name}** (`{watch.id}`).", color=COLOR_VINTED),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # /suivi
    # ------------------------------------------------------------------ #

    suivi = app_commands.Group(name="suivi", description="Suivre un article précis (prix, disponibilité).")

    @suivi.command(name="ajouter", description="Surveille un article précis : baisse de prix, vente, retour.")
    @app_commands.describe(
        url="URL de l'article Vinted (ou son identifiant numérique)",
        canal="Salon où envoyer les alertes (défaut : salon actuel, sinon MP)",
        mention='Mentionner "@here", "everyone" ou un nom de rôle',
        intervalle="Fréquence de vérification",
    )
    async def suivi_ajouter(
        self,
        interaction: discord.Interaction,
        url: str,
        canal: discord.TextChannel | None = None,
        mention: str | None = None,
        intervalle: app_commands.Choice[int] | None = None,
    ) -> None:
        from vinted_scraper import item_id_from_url

        await interaction.response.defer(ephemeral=True)

        item_id = item_id_from_url(url)
        if item_id is None:
            try:
                item_id = int(url.strip())
            except ValueError:
                await interaction.followup.send(
                    embed=_error_embed("Ce n'est pas une URL d'article Vinted ni un identifiant numérique."),
                    ephemeral=True,
                )
                return

        try:
            item = await self.vinted.get_item(item_id)
        except Exception as exc:
            await interaction.followup.send(
                embed=_error_embed(f"Impossible de récupérer l'article #{item_id} : `{exc}`"),
                ephemeral=True,
            )
            return

        target_type, target_id, guild_id = self._target(interaction, canal)
        existing = self.store.get_tracked(item_id)
        if existing is not None:
            existing.target_type, existing.target_id, existing.guild_id = target_type, target_id, guild_id
            if mention:
                existing.mention = mention.strip()[:40]
            self.store.mark_dirty()
        else:
            existing = TrackedItem(
                item_id=item_id,
                url=item.get("url") or f"https://www.vinted.{self.cfg.domain}/items/{item_id}",
                name=str(item.get("title") or f"Article #{item_id}")[:80],
                target_type=target_type,
                target_id=target_id,
                guild_id=guild_id,
                mention=(mention or "").strip()[:40] or None,
                interval=intervalle.value if intervalle else self.cfg.suivi_interval,
                created_at=time.time(),
                last_price=item.get("price"),
                last_closed=bool(item.get("is_closed")),
                last_title=item.get("title"),
            )
            self.store.add_tracked(existing)
        await self.store.save()

        current_price = _num(existing.last_price)
        price_line = (
            f"Prix actuel : {current_price:g} €"
            if current_price is not None
            else "Prix actuel : —"
        )
        description = f"**{existing.name}**\n{price_line}"
        if existing.last_closed:
            description += "\n⚠️ Cet article est déjà **vendu/retiré** : vous serez alerté s'il revient."
        embed = discord.Embed(
            title="✅ Article suivi",
            description=description,
            color=COLOR_VINTED,
        )
        embed.set_footer(text=f"Suivi #{item_id} · fréquence {existing.interval}s")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @suivi.command(name="retirer", description="Arrête de suivre un article.")
    @app_commands.describe(id="Identifiant de l'article (/veille liste)")
    @app_commands.autocomplete(id=_tracked_choices)
    async def suivi_retirer(self, interaction: discord.Interaction, id: str) -> None:
        try:
            item_id = int(id.strip())
        except ValueError:
            await interaction.response.send_message(embed=_error_embed(f"`{id}` n'est pas un identifiant valide."), ephemeral=True)
            return
        tracked = self.store.remove_tracked(item_id)
        if tracked is None:
            await interaction.response.send_message(embed=_error_embed(f"Article #{item_id} non suivi."), ephemeral=True)
            return
        await self.store.save()
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ Suivi arrêté",
                description=f"**{tracked.name}** (#{item_id}) — {tracked.alerts_sent} alertes envoyées.",
                color=COLOR_VINTED,
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # /recherche
    # ------------------------------------------------------------------ #

    @app_commands.command(name="recherche", description="Recherche ponctuelle sur Vinted (résultats les plus récents).")
    @app_commands.describe(
        requete="Texte de recherche ou URL de recherche Vinted",
        prix_min="Prix minimum",
        prix_max="Prix maximum",
        marque="Filtrer par marque (autocomplétion)",
        limite="Nombre de résultats affichés",
    )
    async def recherche(
        self,
        interaction: discord.Interaction,
        requete: str,
        prix_min: float | None = None,
        prix_max: float | None = None,
        marque: str | None = None,
        limite: int = 3,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        filters, error = await self._resolve_filters(requete, prix_min, prix_max, marque)
        if error:
            await interaction.followup.send(embed=_error_embed(error), ephemeral=True)
            return

        limit = max(1, min(self.cfg.max_display, limite))
        try:
            items = await self.vinted.search(filters, page_size=max(12, limit))
            self.store.bump("searches")
            await self.store.save()
        except Exception as exc:
            await interaction.followup.send(
                embed=_error_embed(f"Recherche impossible : `{exc}`"),
                ephemeral=True,
            )
            return

        items = [i for i in items if not i.get("is_closed")][:limit]
        if not items:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔍 Aucun résultat",
                    description=f"Rien pour : {_filters_summary(filters)}",
                    color=COLOR_VINTED,
                ),
                ephemeral=True,
            )
            return

        embeds = [item_embed(item, footer=f"Vintify · {i+1}/{len(items)}") for i, item in enumerate(items)]
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /stats
    # ------------------------------------------------------------------ #

    @app_commands.command(name="stats", description="Statistiques du bot.")
    async def stats(self, interaction: discord.Interaction) -> None:
        uptime = timedelta(seconds=int(time.time() - self.store.started_at))
        active_watches = sum(1 for w in self.store.watches.values() if w.enabled)
        active_tracked = sum(1 for t in self.store.tracked.values() if t.enabled)
        embed = discord.Embed(
            title="📊 Statistiques",
            color=COLOR_VINTED,
            description=(
                f"⏱️ **Uptime** : {uptime}\n"
                f"🌍 **Domaine Vinted** : {self.cfg.domain}\n\n"
                f"👁️ **Veilles** : {active_watches}/{len(self.store.watches)} actives\n"
                f"📌 **Articles suivis** : {active_tracked}/{len(self.store.tracked)} actifs\n\n"
                f"🔎 **Recherches effectuées** : {self.store.stats.get('searches', 0)}\n"
                f"🔔 **Alertes envoyées** : {self.store.stats.get('alerts', 0)}\n"
                f"⚠️ **Erreurs** : {self.store.stats.get('errors', 0)}\n"
                f"📡 **Requêtes Vinted** : {self.vinted.requests}\n"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /aide
    # ------------------------------------------------------------------ #

    @app_commands.command(name="aide", description="Aide générale du bot.")
    async def aide(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🤖 Vintify — Bot d'alertes Vinted",
            description=(
                "Surveille les nouvelles annonces et les baisses de prix, "
                "**sans token de session Vinted** (endpoints publics uniquement : "
                "pas d'offres ni d'achat).\n\n"
                "**📡 Veilles (nouvelles annonces)**\n"
                "• `/veille ajouter` — texte ou URL de recherche, avec prix/marque\n"
                "• `/veille liste`, `/veille retirer`, `/veille pause`, `/veille reprendre`\n\n"
                "**📌 Suivi d'articles**\n"
                "• `/suivi ajouter <url>` — alerte sur baisse de prix, vente ou retour\n"
                "• `/suivi retirer`\n\n"
                "**🔍 Recherche ponctuelle**\n"
                "• `/recherche` — résultats immédiats\n\n"
                "**ℹ️** `/stats` · `/aide`\n\n"
                "> ⚠️ L'API Vinted est privée et non documentée : utilisez ce bot "
                "avec modération (intervalles ≥ 30 s) et dans le respect des "
                "conditions d'utilisation de Vinted."
            ),
            color=COLOR_VINTED,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
