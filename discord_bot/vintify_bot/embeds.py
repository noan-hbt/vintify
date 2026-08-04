"""Construction des embeds Discord (français) pour les alertes et résultats."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

from vintify_bot.vinted import status_title

COLOR_VINTED = 0x09B1BA   # bleu Vinted
COLOR_SOLD = 0xE74C3C     # rouge
COLOR_DROP = 0x2ECC71     # vert

_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "SEK": "kr"}


def _ts(value: Any) -> datetime | None:
    """Convertit un timestamp (epoch ou ISO) en datetime UTC, si possible."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _money(price: Any, currency: str | None) -> str:
    if isinstance(price, dict):          # format objet {"amount": …}
        price = price.get("amount")
    if price is None:
        return "—"
    try:
        symbol = _CURRENCY_SYMBOLS.get((currency or "").upper(), (currency or "").upper())
        return f"{float(price):g} {symbol}".strip()
    except (TypeError, ValueError):
        return "—"


def _seller(item: dict[str, Any]) -> str:
    return str(item.get("user_login") or item.get("user_id") or "Inconnu")


def item_embed(item: dict[str, Any], *, footer: str | None = None) -> discord.Embed:
    """Embed standard pour une annonce (alerte de veille ou résultat de recherche)."""
    title = str(item.get("title") or "Annonce Vinted")[:256]
    closed = bool(item.get("is_closed"))
    embed = discord.Embed(
        title=title,
        url=item.get("url"),
        color=COLOR_SOLD if closed else COLOR_VINTED,
        description="🔴 **Vendu / retiré**" if closed else None,
    )
    photo = item.get("photo")
    if photo:
        embed.set_image(url=photo)

    price = item.get("price")
    currency = item.get("currency")
    embed.add_field(name="💰 Prix", value=_money(price, currency), inline=True)
    total = item.get("total_price")
    if total is not None and price is not None and abs(float(total) - float(price)) > 0.005:
        embed.add_field(name="🧾 Total (frais inclus)", value=_money(total, currency), inline=True)
    if item.get("size"):
        embed.add_field(name="📏 Taille", value=str(item["size"])[:200], inline=True)
    if item.get("status"):
        embed.add_field(name="🧵 État", value=status_title(item["status"])[:200], inline=True)
    if item.get("brand"):
        embed.add_field(name="🏷️ Marque", value=str(item["brand"])[:200], inline=True)
    if item.get("favourites") is not None or item.get("views") is not None:
        fav = item.get("favourites") or 0
        views = item.get("views") or 0
        embed.add_field(name="❤️ / 👁️", value=f"{fav} · {views}", inline=True)
    embed.add_field(name="👤 Vendeur", value=_seller(item)[:200], inline=True)
    if item.get("country"):
        embed.add_field(name="🌍 Pays", value=str(item["country"])[:200], inline=True)

    created = _ts(item.get("created_at"))
    embed.timestamp = created or discord.utils.utcnow()
    embed.set_footer(text=(footer or "Vintify")[:200])
    return embed


def new_item_alert(item: dict[str, Any], watch) -> discord.Embed:
    """Alerte : nouvelle annonce correspondant à une veille."""
    embed = item_embed(
        item,
        footer=f"Veille « {watch.name} » · {watch.id} · Vintify",
    )
    return embed


def price_drop_alert(item: dict[str, Any], tracked, old_price: float, new_price: float) -> discord.Embed:
    """Alerte : baisse de prix sur un article suivi."""
    embed = discord.Embed(
        title=f"📉 Baisse de prix : {item.get('title') or 'article suivi'}",
        url=item.get("url") or tracked.url,
        color=COLOR_DROP,
    )
    photo = item.get("photo")
    if photo:
        embed.set_image(url=photo)
    currency = item.get("currency")
    embed.add_field(name="Avant", value=_money(old_price, currency), inline=True)
    embed.add_field(name="Maintenant", value=f"**{_money(new_price, currency)}**", inline=True)
    if item.get("size"):
        embed.add_field(name="Taille", value=str(item["size"])[:200], inline=True)
    embed.add_field(name="Vendeur", value=_seller(item)[:200], inline=True)
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"Suivi #{tracked.item_id} · Vintify")
    return embed


def sold_alert(item: dict[str, Any], tracked, *, removed: bool = False) -> discord.Embed:
    """Alerte : l'article a été vendu / retiré / n'existe plus."""
    label = "❌ Annonce introuvable" if removed else "🔴 Vendu ou retiré"
    embed = discord.Embed(
        title=f"{label} : {item.get('title') or tracked.name or 'article suivi'}",
        url=item.get("url") or tracked.url,
        color=COLOR_SOLD,
        description="Le suivi est automatiquement désactivé.",
    )
    if tracked.last_price is not None:
        embed.add_field(name="Dernier prix connu", value=_money(tracked.last_price, "EUR"), inline=True)
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"Suivi #{tracked.item_id} · Vintify")
    return embed


def reopened_alert(item: dict[str, Any], tracked) -> discord.Embed:
    """Alerte : l'article est de nouveau disponible."""
    embed = discord.Embed(
        title=f"🟢 De retour : {item.get('title') or tracked.name or 'article suivi'}",
        url=item.get("url") or tracked.url,
        color=COLOR_VINTED,
    )
    embed.add_field(name="Prix", value=_money(item.get("price"), item.get("currency")), inline=True)
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"Suivi #{tracked.item_id} · Vintify")
    return embed
