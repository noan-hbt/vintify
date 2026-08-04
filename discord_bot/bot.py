#!/usr/bin/env python3
"""Point d'entrée du bot Discord Vintify.

Usage :
    python bot.py                     # démarre le bot Discord (DISCORD_TOKEN requis)
    python bot.py --search "nike tn" --price-to 60
                                      # recherche rapide sans token (test déploiement)
    python bot.py --selftest          # vérifie environnement + une recherche live

Déploiement Railway : voir README.md (Procfile : ``worker: python bot.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Le module vinted_scraper.py vit à la racine du dépôt (parents[1]).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from vintify_bot.config import Config  # noqa: E402
from vintify_bot.cogs import VintifyCog  # noqa: E402
from vintify_bot.poller import Poller  # noqa: E402
from vintify_bot.storage import Store  # noqa: E402
from vintify_bot.vinted import VintedService  # noqa: E402

log = logging.getLogger("vintify")


# --------------------------------------------------------------------------- #
# Mode CLI (aucun token requis)
# --------------------------------------------------------------------------- #

def run_cli(args: argparse.Namespace) -> int:
    """Recherche rapide pour valider un déploiement sans bot Discord."""
    from vinted_scraper import SearchFilters, Sort, VintedScraper

    cfg = Config.from_env()
    print(f"Domaine Vinted : {cfg.domain}")
    print(f"Recherche : {args.search!r} (prix {args.price_from} → {args.price_to})")

    filters = SearchFilters(
        search_text=args.search,
        price_from=args.price_from,
        price_to=args.price_to,
        order=Sort.NEWEST.value,
    )
    scraper = VintedScraper(domain=cfg.domain, timeout=30, retries=4, min_request_interval=0.25)
    try:
        items = scraper.search_items(
            filters=filters,
            pages=1,
            page_size=max(5, min(96, args.pages * 12)),
            normalize=True,
        )
    finally:
        scraper.close()

    for i, item in enumerate(items, start=1):
        price = item.get("price")
        print(f"{i:>2}. [{price:g} {item.get('currency') or ''}] {item.get('title')}")
        print(f"     {item.get('url')}")
    print(f"\n{len(items)} résultat(s).")
    return 0


def run_selftest() -> int:
    """Vérifie la configuration, la persistance et une requête live."""
    cfg = Config.from_env()
    ok = True

    if not cfg.token:
        print("[WARN] DISCORD_TOKEN absent : le mode bot ne démarrera pas "
              "(le mode CLI reste utilisable).")
    else:
        print("[OK] DISCORD_TOKEN présent.")

    store = Store(cfg.data_dir / "state.json")
    store.load()
    store.mark_dirty()
    asyncio.run(store.save())
    print(f"[OK] Persistance {store.path} (répertoire : {cfg.data_dir}).")

    from vinted_scraper import SearchFilters, VintedScraper

    scraper = VintedScraper(domain=cfg.domain, timeout=30, retries=3, min_request_interval=0.25)
    try:
        items = scraper.search_items(
            filters=SearchFilters(search_text="nike", price_to=80),
            pages=1,
            page_size=5,
            normalize=True,
        )
        print(f"[OK] Recherche live Vinted : {len(items)} résultat(s).")
        for item in items[:3]:
            print(f"     - [{item.get('price')} {item.get('currency')}] {item.get('title')}")
    except Exception as exc:
        print(f"[FAIL] Recherche live impossible : {exc}")
        ok = False
    finally:
        scraper.close()

    print("\nSELFTEST " + ("OK" if ok else "PARTIEL"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Mode Discord
# --------------------------------------------------------------------------- #

async def run_bot(cfg: Config) -> int:
    store = Store(cfg.data_dir / "state.json")
    store.load()
    vinted = VintedService(domain=cfg.domain, page_size=cfg.page_size)
    await vinted.start()

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
    poller = Poller(bot, store, vinted, cfg)
    await bot.add_cog(VintifyCog(bot, store, vinted, poller, cfg))

    @bot.event
    async def on_ready() -> None:
        log.info("Connecté en tant que %s (id %s)", bot.user, bot.user.id)
        log.info("Serveurs : %s", ", ".join(g.name for g in bot.guilds) or "aucun")
        try:
            synced = await bot.tree.sync()
            log.info("Commandes slash synchronisées : %d", len(synced))
        except Exception as exc:
            log.warning("Synchronisation des commandes impossible : %s", exc)
        await poller.start()

    @bot.event
    async def on_error(event: str, *args, **kwargs) -> None:
        log.exception("Erreur d'événement %s", event)

    try:
        await bot.start(cfg.token)
    except discord.LoginFailure:
        print("Échec de connexion : DISCORD_TOKEN invalide.")
        return 1
    finally:
        await poller.stop()
        await store.save()
        await vinted.close()
    return 0


def main() -> int:
    # Console Windows (cp1252) : force l'UTF-8 pour les caractères accentués
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Bot Discord Vintify (alertes Vinted).")
    parser.add_argument("--search", metavar="TEXTE", help="recherche rapide sans bot (test)")
    parser.add_argument("--price-from", type=float, default=None)
    parser.add_argument("--price-to", type=float, default=None)
    parser.add_argument("--pages", type=int, default=2, help="pages pour --search")
    parser.add_argument("--selftest", action="store_true", help="auto-test du déploiement")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    if args.search:
        return run_cli(args)

    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Vintify %s — démarrage (domaine %s, données %s).",
             __import__("vintify_bot").__version__, cfg.domain, cfg.data_dir)

    if not cfg.token:
        print("DISCORD_TOKEN manquant. Définissez-le (Railway → Variables) ou "
              "utilisez --search / --selftest.")
        return 2

    if sys.platform != "win32":
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def _shutdown(signum, _frame) -> None:
            log.info("Signal %s reçu : arrêt propre…", signum)
            for task in asyncio.all_tasks(loop):
                task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _shutdown, sig, None)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            return loop.run_until_complete(run_bot(cfg))
        except asyncio.CancelledError:
            return 0
    else:
        return asyncio.run(run_bot(cfg))


if __name__ == "__main__":
    sys.exit(main())
