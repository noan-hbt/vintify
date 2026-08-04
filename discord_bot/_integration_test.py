"""Test d'intégration du poller : veille (dédup), baisse de prix, 404 réel."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "discord_bot")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine du dépôt (vinted_scraper.py)

from vintify_bot.config import Config
from vintify_bot.poller import Poller
from vintify_bot.storage import Store, TrackedItem, Watch
from vintify_bot.vinted import VintedService


class FakeUser:
    def __init__(self, id):
        self.id = id

    async def send(self, content=None, embed=None, view=None):
        FakeChannel.record(embed, content)


class FakeChannel:
    sent = []

    def __init__(self, id):
        self.id = id

    @classmethod
    def record(cls, embed, content):
        cls.sent.append((embed.title if embed else None, embed.url if embed else None, content))

    async def send(self, content=None, embed=None, view=None):
        self.record(embed, content)


class FakeBot:
    def __init__(self):
        self.channels = {111: FakeChannel(111)}
        self.users = {222: FakeUser(222)}

    async def wait_until_ready(self):
        pass

    def get_channel(self, id):
        return self.channels.get(id)

    async def fetch_channel(self, id):
        return self.channels[id]

    def get_user(self, id):
        return self.users.get(id)

    async def fetch_user(self, id):
        return self.users[id]

    def get_guild(self, id):
        return None


async def main():
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(token="", data_dir=tmp, page_size=12)
    store = Store(tmp / "state.json")
    store.load()
    vinted = VintedService("fr", page_size=12)
    await vinted.start()
    bot = FakeBot()
    poller = Poller(bot, store, vinted, cfg)

    # --- 1) veille : premier scan puis déduplication ---------------------
    watch = Watch(
        id="wtest1", name="nike tn", filters={"search_text": "nike tn", "price_to": 80},
        target_type="channel", target_id=111, interval=60, created_at=time.time(),
    )
    store.add_watch(watch)
    await poller._poll_watch(watch)
    n1 = len(FakeChannel.sent)
    print(f"[1] scan initial : {n1} alertes")
    assert n1 > 0, "aucune alerte au premier scan"
    watch.last_run = None
    await poller._poll_watch(watch)
    n2 = len(FakeChannel.sent) - n1
    print(f"[1] second scan : {n2} nouvelles (attendu 0)")
    assert n2 == 0, "déduplication en échec"

    # --- 2) baisse de prix sur un article réel ---------------------------
    items = await vinted.search({"search_text": "nike tn", "price_to": 80}, page_size=5)
    live = items[0]
    live_price = float(live["price"])
    tracked = TrackedItem(
        item_id=live["id"], url=live.get("url") or "", name=live["title"],
        target_type="dm", target_id=222, last_price=live_price * 3, last_closed=False,
    )
    store.add_tracked(tracked)
    await poller._poll_tracked(tracked)
    titles = [s[0] for s in FakeChannel.sent[n1 + n2:]]
    print(f"[2] article {live['id']} à {live_price} EUR (dernier suivi {live_price * 3} EUR) -> {titles}")
    assert any(t and "Baisse" in t for t in titles), "baisse non détectée"
    before = len(FakeChannel.sent)
    await poller._poll_tracked(tracked)
    assert len(FakeChannel.sent) == before, "double alerte de baisse"
    print("[2] pas de doublon au repoll : OK")

    # --- 3) article vraiment inexistant : alerte + désactivation ---------
    gone = TrackedItem(
        item_id=4123456789, url="", name="article fantôme",
        target_type="dm", target_id=222, last_price=45.0,
    )
    store.add_tracked(gone)
    await poller._poll_tracked(gone)
    titles = [s[0] for s in FakeChannel.sent[len(titles) + n1 + n2:]]
    print(f"[3] article inexistant -> {titles}")
    assert any(t and "introuvable" in t for t in titles), "pas d'alerte 404"
    assert gone.enabled is False, "suivi non désactivé"
    print("[3] alerte 404 + désactivation : OK")

    print("== integration poller OK ==")


asyncio.run(main())
