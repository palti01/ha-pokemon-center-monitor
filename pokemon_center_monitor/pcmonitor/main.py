"""Pokémon Center (DE) Drop-Monitor – Hauptschleife.

Aufruf:
  python -m pcmonitor                  # Dauerbetrieb
  python -m pcmonitor --once           # ein Durchlauf mit Diagnose, keine Pushs
  python -m pcmonitor --test-notify    # Testnachricht an alle Kanäle
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

import requests
import yaml

from .notify import Event, Notifier
from .shop import FeedItem, ShopClient, fetch_feed, parse_listing, parse_product  # noqa: F401
from .store import Product, Store

log = logging.getLogger("pcmonitor")

DEFAULTS = {
    "listings": [
        "https://www.pokemoncenter.com/de-de/category/tcg-cards",
        "https://www.pokemoncenter.com/de-de/category/elite-trainer-box",
        "https://www.pokemoncenter.com/de-de/category/collections",
    ],
    "watch": [],                       # einzelne Produkt-URLs, die gezielt geprüft werden
    "include_keywords": [],            # leer = alles melden
    "exclude_keywords": [],
    "min_request_gap_s": 45,           # Mindestabstand zwischen ZWEI Shop-Abrufen (gesamt)
    "jitter_s": 15,
    "listing_interval_s": 120,         # jede Liste spätestens alle X s
    "product_recheck_s": 900,          # bekannte Produkte alle X s auf Restock prüfen
    "max_backoff_s": 900,
    "blocked_alert_after_s": 1200,     # Push, wenn der Monitor so lange "blind" ist
    "queue_alert_cooldown_s": 1800,
    "feeds": [
        {"url": "https://pokezentrum.de/feed/",
         "keywords": ["pokémon center", "pokemon center", "vorbestell", "vorverkauf", "drop"]},
        {"url": "https://pokejungle.net/feed/",
         "keywords": ["pokémon center", "pokemon center", "vorbestell", "vorverkauf"]},
    ],
    "feed_interval_s": 300,
    "shop_enabled": True,
    "state_path": "/data/state.db",
    "notify": {},
}


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg.update(user)
    # Betrieb als Home-Assistant-App: Optionen aus der App-Konfiguration
    ha_opts = Path("/data/options.json")
    if ha_opts.exists():
        import json
        o = json.loads(ha_opts.read_text(encoding="utf-8"))
        for k in ("shop_enabled", "listings", "watch", "include_keywords", "exclude_keywords", "min_request_gap_s",
                  "listing_interval_s", "product_recheck_s", "feed_interval_s", "feed_must_contain"):
            if k in o and o[k] is not None:
                cfg[k] = o[k]
        if o.get("feed_urls") is not None:
            feeds = []
            for entry in o["feed_urls"]:
                # Format: "URL" oder "URL | stichwort1, stichwort2" (eigene Stichworte, ohne Pflichtwörter)
                url, _, kw = entry.partition("|")
                f = {"url": url.strip(), "keywords": o.get("feed_keywords") or []}
                if kw.strip():
                    f["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]
                    f["own_keywords"] = True
                feeds.append(f)
            cfg["feeds"] = feeds
        n = {}
        if o.get("webhook_url"):
            n["homeassistant"] = {"webhook_url": o["webhook_url"]}
        if o.get("reddit_client_id") and o.get("reddit_client_secret"):
            cfg["reddit"] = {"client_id": o["reddit_client_id"], "client_secret": o["reddit_client_secret"]}
        if o.get("ntfy_url"):
            n["ntfy"] = {"url": o["ntfy_url"]}
        cfg["notify"] = n
        cfg["state_path"] = "/data/state.db"
    # Geheimnisse optional per Umgebungsvariablen
    n = cfg.setdefault("notify", {}) or {}
    if os.getenv("HA_WEBHOOK_URL"):
        n.setdefault("homeassistant", {})["webhook_url"] = os.environ["HA_WEBHOOK_URL"]
    if os.getenv("NTFY_URL"):
        n.setdefault("ntfy", {})["url"] = os.environ["NTFY_URL"]
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        n["telegram"] = {"bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
                         "chat_id": os.environ["TELEGRAM_CHAT_ID"]}
    cfg["notify"] = n
    return cfg


AV_DE = {"in_stock": "verfügbar", "out_of_stock": "ausverkauft",
         "coming_soon": "demnächst", "unknown": "unbekannt"}


class Monitor:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry = dry_run
        self.store = Store(cfg["state_path"])
        self.notifier = Notifier(cfg["notify"])
        self.shop = ShopClient()
        self.feed_session = requests.Session()
        if (cfg.get("reddit") or {}).get("client_id"):
            from . import shop as _shop
            _shop.REDDIT = _shop.RedditAuth(cfg["reddit"]["client_id"], cfg["reddit"]["client_secret"])
            log.info("Reddit über offizielle API (OAuth).")
        self.listing_due = {u: 0.0 for u in cfg["listings"]}
        self.backoff = 0.0
        self.blocked_since: float | None = None
        self.blocked_alerted = False
        self.last_queue_alert = 0.0
        self.last_feed = 0.0
        self.baseline_done = self.store.meta_get("baseline") == "1"
        self.listing_ok: set[str] = set()
        self.feeds_logged: set[str] = set()
        self.feed_host_last: dict[str, float] = {}
        self.running = True

    # ---------- Filter ----------------------------------------------------
    def wanted(self, title: str) -> bool:
        t = title.lower()
        inc = [k.lower() for k in self.cfg.get("include_keywords") or []]
        exc = [k.lower() for k in self.cfg.get("exclude_keywords") or []]
        if exc and any(k in t for k in exc):
            return False
        return not inc or any(k in t for k in inc)

    def emit(self, ev: Event) -> None:
        if self.dry:
            log.info("[dry-run] %s: %s %s", ev.headline, ev.body(), ev.url)
        else:
            self.notifier.send(ev)

    # ---------- Shop ------------------------------------------------------
    def handle_state(self, state: str, retry_after: float, url: str) -> bool:
        """True, wenn der Abruf normal war."""
        now = time.time()
        if state == "ok":
            if self.blocked_since and self.blocked_alerted:
                self.emit(Event("status", "Monitor sieht den Shop wieder ✔︎"))
            self.backoff, self.blocked_since, self.blocked_alerted = 0.0, None, False
            return True

        if state == "queue":
            log.warning("Warteraum aktiv (%s)", url)
            if now - self.last_queue_alert > self.cfg["queue_alert_cooldown_s"]:
                self.last_queue_alert = now
                self.emit(Event("queue", "Der Shop leitet in die Warteschlange – jetzt rein!",
                                url="https://www.pokemoncenter.com/de-de"))
        else:
            log.warning("Shop-Abruf: %s (%s)", state, url)

        self.blocked_since = self.blocked_since or now
        self.backoff = min(max(self.backoff * 2, 60), self.cfg["max_backoff_s"])
        self.backoff = max(self.backoff, retry_after)
        if (state != "queue" and not self.blocked_alerted
                and now - self.blocked_since > self.cfg["blocked_alert_after_s"]):
            self.blocked_alerted = True
            self.emit(Event("status", f"Monitor ist seit {int((now - self.blocked_since) / 60)} min "
                                    f"blockiert ({state}) – bitte manuell im Auge behalten."))
        return False

    def check_listing(self, url: str) -> None:
        res = self.shop.fetch(url)
        if not self.handle_state(res.state, res.retry_after, url):
            return
        products = parse_listing(res.html, res.final_url or url)
        log.info("Liste %s: %d Produkte", url.rsplit("/", 1)[-1], len(products))
        if not products:
            log.warning("Keine Produkte erkannt – Seitenaufbau geändert oder JS-only? (%s)", url)
            return
        self.listing_ok.add(url)
        for p in products:
            old = self.store.get(p.url)
            self.store.upsert(p)
            if old is None and self.baseline_done and self.wanted(p.title):
                self.emit(Event("new", p.title, url=p.url, price=p.price,
                                availability=AV_DE.get(p.availability or "unknown", ""), image=p.image))
            elif old and p.availability and old.availability != p.availability:
                self._availability_changed(old, p)

    def check_product(self, url: str) -> None:
        res = self.shop.fetch(url)
        if not self.handle_state(res.state, res.retry_after, url):
            return
        p = parse_product(res.html, url)
        old = self.store.get(p.url) or self.store.get(url)
        self.store.upsert(p, checked=True)
        log.info("Produkt %s: %s %s", p.sku, AV_DE.get(p.availability, p.availability), p.price)
        if old and p.availability != "unknown":
            self._availability_changed(old, p)

    def _availability_changed(self, old: Product, new: Product) -> None:
        if (new.availability == "in_stock" and old.availability in ("out_of_stock", "coming_soon")
                and self.wanted(new.title or old.title)):
            self.emit(Event("restock", new.title or old.title, url=new.url,
                            price=new.price or old.price, availability="jetzt bestellbar",
                            image=new.image or old.image))

    def next_shop_task(self):
        now = time.time()
        due = [u for u, t in self.listing_due.items() if t <= now]
        if due:
            u = min(due, key=lambda x: self.listing_due[x])
            self.listing_due[u] = now + self.cfg["listing_interval_s"]
            return self.check_listing, u
        for w in self.cfg.get("watch") or []:
            if self.store.get(w) is None:
                self.store.upsert(Product(url=w))
        todo = self.store.due_for_check(self.cfg["product_recheck_s"], 1)
        if todo:
            return self.check_product, todo[0]
        return None, None

    # ---------- Feeds -----------------------------------------------------
    def check_feeds(self) -> None:
        self.last_feed = time.time()
        must = [k.lower() for k in self.cfg.get("feed_must_contain") or []]
        for feed in self.cfg.get("feeds") or []:
            url = feed["url"]
            host = url.split("/")[2] if "//" in url else url
            wait = self.feed_host_last.get(host, 0) + 10 - time.time()
            if wait > 0:              # gleiche Seite nicht direkt hintereinander abfragen (Reddit: 429)
                self._sleep(wait)
            self.feed_host_last[host] = time.time()
            try:
                items = fetch_feed(self.feed_session, url)
            except Exception as e:  # noqa: BLE001
                log.warning("Feed %s nicht lesbar: %s", url, e)
                continue
            if url not in self.feeds_logged:
                self.feeds_logged.add(url)
                log.info("Feed ok: %s – %d Einträge, neuester: %s", url, len(items),
                         items[0].title[:80] if items else "-")
            kws = [k.lower() for k in feed.get("keywords") or []]
            key = "feed2:" + url
            seen_list = [g for g in self.store.meta_get(key).split("\n") if g]
            seen = set(seen_list)
            first = not seen
            for it in items:
                if it.guid in seen:
                    continue
                seen.add(it.guid)
                seen_list.append(it.guid)
                t = it.title.lower()
                if must and not feed.get("own_keywords") and not any(k in t for k in must):
                    continue
                if first:
                    if not kws or any(k in t for k in kws):
                        log.info("Baseline-Treffer [%s] %s – %s", it.published or "?", host, it.title[:120])
                    continue
                if not kws or any(k in t for k in kws):
                    self.emit(Event("new", f"📰 {it.title}", url=it.link, extra={"source": url}))
            self.store.meta_set(key, "\n".join(seen_list[-500:]))

    # ---------- Schleife --------------------------------------------------
    def run_once_diagnostic(self) -> int:
        ok = 0
        for u in self.cfg["listings"]:
            res = self.shop.fetch(u)
            prods = parse_listing(res.html, res.final_url or u) if res.state == "ok" else []
            print(f"[{res.state:>12}] HTTP {res.status}  {len(prods):3d} Produkte  {u}")
            for p in prods[:5]:
                print(f"               - {p.sku}  {p.title[:70]}  {p.price}")
            ok += res.state == "ok" and bool(prods)
            time.sleep(self.cfg["min_request_gap_s"] / 3)
        for feed in self.cfg.get("feeds") or []:
            try:
                items = fetch_feed(self.feed_session, feed["url"])
                print(f"[          ok] Feed {feed['url']}: {len(items)} Einträge, neuester: "
                      f"{items[0].title if items else '-'}")
            except Exception as e:  # noqa: BLE001
                print(f"[       error] Feed {feed['url']}: {e}")
        return 0 if ok else 2

    def run(self) -> None:
        if not self.cfg.get("shop_enabled", True):
            log.info("Shop-Abruf deaktiviert – nur News-Feeds werden überwacht.")
        log.info("Start – %d bekannte Produkte, Baseline %s",
                 self.store.count(), "vorhanden" if self.baseline_done else "wird angelegt")
        while self.running:
            if time.time() - self.last_feed > self.cfg["feed_interval_s"]:
                self.check_feeds()

            if not self.cfg.get("shop_enabled", True):
                self._sleep(self.cfg["feed_interval_s"])
                continue

            if self.backoff:
                log.info("Pause %ds (Backoff)", self.backoff)
                self._sleep(self.backoff)
                # nach dem Backoff die erste Liste erneut versuchen
                first = next(iter(self.listing_due))
                self.listing_due[first] = 0

            fn, arg = self.next_shop_task()
            if fn:
                try:
                    fn(arg)
                except Exception:  # noqa: BLE001
                    log.exception("Fehler bei %s", arg)

            if not self.baseline_done and set(self.listing_due) <= self.listing_ok:
                self.baseline_done = True
                self.store.meta_set("baseline", "1")
                log.info("Baseline steht (%d Produkte) – ab jetzt wird gemeldet.", self.store.count())

            self._sleep(self.cfg["min_request_gap_s"] + random.uniform(0, self.cfg["jitter_s"]))

    def _sleep(self, s: float) -> None:
        end = time.time() + s
        while self.running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def stop(self, *_):
        self.running = False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pokémon Center (DE) Drop-Monitor")
    ap.add_argument("-c", "--config", default=os.getenv("PCM_CONFIG", "/config/config.yaml"))
    ap.add_argument("--once", action="store_true", help="ein Diagnose-Durchlauf, keine Pushs")
    ap.add_argument("--test-notify", action="store_true", help="Testnachricht senden")
    ap.add_argument("--dry-run", action="store_true", help="Events nur loggen")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%d.%m. %H:%M:%S", stream=sys.stdout)
    cfg = load_config(a.config)
    cfg["min_request_gap_s"] = max(30, float(cfg["min_request_gap_s"]))  # Schutz vor zu hoher Last

    if a.test_notify:
        Notifier(cfg["notify"]).send(Event(
            "test", "Pokémon-Center-Monitor ist eingerichtet",
            url="https://www.pokemoncenter.com/de-de", price="0,00 €", availability="Test"))
        return 0

    m = Monitor(cfg, dry_run=a.dry_run or a.once)
    if a.once:
        return m.run_once_diagnostic()
    signal.signal(signal.SIGTERM, m.stop)
    signal.signal(signal.SIGINT, m.stop)
    m.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
