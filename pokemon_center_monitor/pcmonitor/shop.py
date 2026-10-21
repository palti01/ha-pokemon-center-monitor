"""Abruf und Auswertung des Pokémon-Center-Shops (de-de) sowie News-Feeds.

Grundsatz: Wir holen nur öffentliche Seiten, so selten wie sinnvoll, und
werten Bot-Schutz-/Warteraum-Seiten als eigenen Zustand aus – niemals als
"ausverkauft" und ohne zu versuchen, den Schutz zu umgehen.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .store import Product

log = logging.getLogger("pcmonitor.shop")

BASE = "https://www.pokemoncenter.com"
PRODUCT_RE = re.compile(r"/(?:de-de|en-de)/product/(\d{2,3}-\d{4,6}-\d{2,4})(?:/([^/?#\"'\s]+))?", re.I)

# Hinweise auf den Imperva-Warteraum (Warteschlange) bzw. eine Bot-Abfrage.
QUEUE_HINTS = ("waiting room", "warteraum", "warteschlange", "you are in line", "in der schlange",
               "virtual queue", "queue-it", "high demand", "hohe nachfrage", "hohen nachfrage")
BLOCK_HINTS = ("_incapsula_resource", "incapsula incident", "request unsuccessful",
               "access denied", "zugriff verweigert", "error 15", "error 17", "captcha")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


@dataclass
class FetchResult:
    state: str            # "ok" | "queue" | "blocked" | "rate_limited" | "error"
    status: int = 0
    html: str = ""
    retry_after: float = 0.0
    final_url: str = ""


def classify(status: int, html: str, final_url: str = "") -> str:
    low = html[:200_000].lower()
    host = urlparse(final_url).netloc.lower()
    if "queue" in host or any(h in low for h in QUEUE_HINTS):
        # Warteraum-Seiten sind klein und enthalten keine Produktliste.
        if not PRODUCT_RE.search(html) or "queue" in host:
            return "queue"
    if status == 429:
        return "rate_limited"
    if status in (401, 403) or (any(h in low for h in BLOCK_HINTS) and not PRODUCT_RE.search(html)):
        return "blocked"
    if status >= 400:
        return "error"
    return "ok"


class ShopClient:
    def __init__(self, timeout: float = 20):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        })
        self.timeout = timeout

    def fetch(self, url: str) -> FetchResult:
        try:
            r = self.s.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as e:
            log.warning("Abruf fehlgeschlagen %s: %s", url, e)
            return FetchResult("error")
        ra = 0.0
        if r.headers.get("Retry-After", "").isdigit():
            ra = float(r.headers["Retry-After"])
        state = classify(r.status_code, r.text, r.url)
        return FetchResult(state, r.status_code, r.text, ra, r.url)


# --------------------------------------------------------------------------
# Listen-/Suchseiten
# --------------------------------------------------------------------------
def sku_of(url: str) -> str:
    m = PRODUCT_RE.search(url)
    return m.group(1) if m else ""


def canonical(url: str) -> str:
    m = PRODUCT_RE.search(url)
    if not m:
        return url
    slug = f"/{m.group(2)}" if m.group(2) else ""
    loc = "en-de" if "/en-de/" in url.lower() else "de-de"
    return f"{BASE}/{loc}/product/{m.group(1)}{slug}"


def _title_from_slug(url: str) -> str:
    m = PRODUCT_RE.search(url)
    if not m or not m.group(2):
        return m.group(1) if m else url
    return m.group(2).replace("-", " ").title()


def parse_listing(html: str, page_url: str = BASE) -> list[Product]:
    """Alle Produktlinks einer Kategorie-/Suchseite (dedupliziert nach SKU)."""
    found: dict[str, Product] = {}
    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        sku = sku_of(href)
        if not sku:
            continue
        url = canonical(href)
        title = " ".join(a.get_text(" ", strip=True).split())
        p = found.get(sku) or Product(url=url, sku=sku)
        if len(title) > len(p.title) and len(title) < 200:
            p.title = title
        if p.url.count("/") < url.count("/"):   # URL mit Slug bevorzugen
            p.url = url
        found[sku] = p

    # Next.js liefert die Liste oft (auch) als JSON in __NEXT_DATA__.
    for obj in _iter_next_data_objects(soup):
        url = obj.get("url") or obj.get("href") or obj.get("path") or ""
        sku = obj.get("sku") or obj.get("pid") or sku_of(url)
        if not sku or not re.fullmatch(r"\d{2,3}-\d{4,6}-\d{2,4}", str(sku)):
            continue
        sku = str(sku)
        p = found.get(sku) or Product(url=canonical(urljoin(page_url, url)) if url else
                                       f"{BASE}/de-de/product/{sku}", sku=sku)
        p.title = p.title or str(obj.get("name") or obj.get("title") or "")
        price = _price_str(obj.get("price"))
        p.price = p.price or price
        p.availability = p.availability or _map_availability(
            str(obj.get("availability") or obj.get("availability_status") or obj.get("stockStatus") or ""))
        found[sku] = p

    for p in found.values():
        if not p.title:
            p.title = _title_from_slug(p.url)
    return list(found.values())


# --------------------------------------------------------------------------
# Produktseite
# --------------------------------------------------------------------------
def parse_product(html: str, url: str) -> Product:
    soup = BeautifulSoup(html, "lxml")
    p = Product(url=canonical(url), sku=sku_of(url))

    # 1) schema.org JSON-LD
    for tag in soup.find_all("script", type="application/ld+json"):
        for obj in _walk(_loads(tag.string or tag.get_text())):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                p.title = p.title or str(obj.get("name") or "")
                p.sku = p.sku or str(obj.get("sku") or "")
                img = obj.get("image")
                p.image = p.image or (img[0] if isinstance(img, list) and img else img or "")
                offers = obj.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    p.availability = _map_availability(str(offers.get("availability") or ""))
                    if offers.get("price"):
                        p.price = _price_str({"amount": offers.get("price"),
                                              "currency": offers.get("priceCurrency", "EUR")})

    # 2) Next.js-Daten
    if not p.availability or p.availability == "unknown":
        for obj in _iter_next_data_objects(soup):
            for key in ("availability", "availability_status", "stockStatus", "inventoryStatus"):
                if obj.get(key):
                    av = _map_availability(str(obj[key]))
                    if av != "unknown":
                        p.availability = av
                        break
            if obj.get("addToCartForm") and (not p.availability or p.availability == "unknown"):
                p.availability = "in_stock"
            p.title = p.title or str(obj.get("name") or "")
            p.price = p.price or _price_str(obj.get("price"))

    # 3) Text-Heuristik (Buttons)
    if not p.availability or p.availability == "unknown":
        p.availability = _availability_from_text(soup)

    if not p.title:
        h1 = soup.find("h1")
        p.title = h1.get_text(" ", strip=True) if h1 else _title_from_slug(url)
    if not p.image:
        og = soup.find("meta", property="og:image")
        p.image = og["content"] if og and og.get("content") else ""
    return p


def _availability_from_text(soup: BeautifulSoup) -> str:
    buttons = " | ".join(b.get_text(" ", strip=True).lower() for b in soup.find_all(["button", "a"]))
    if any(k in buttons for k in ("in den warenkorb", "add to cart", "vorbestellen", "pre-order", "jetzt kaufen")):
        return "in_stock"
    if any(k in buttons for k in ("ausverkauft", "sold out", "benachrichtig", "notify me", "nicht verfügbar")):
        return "out_of_stock"
    if any(k in buttons for k in ("demnächst", "coming soon", "bald erhältlich")):
        return "coming_soon"
    return "unknown"


def _map_availability(v: str) -> str:
    v = v.lower().rsplit("/", 1)[-1].replace("_", "").replace("-", "")
    if v in ("instock", "available", "lowstock", "limitedavailability", "preorder", "presale", "onlineonly"):
        return "in_stock"
    if v in ("outofstock", "soldout", "unavailable", "notavailable", "discontinued", "alertme"):
        return "out_of_stock"
    if v in ("comingsoon", "backorder"):
        return "coming_soon"
    return "unknown"


def _price_str(price) -> str:
    if price is None or price == "":
        return ""
    if isinstance(price, (int, float)):
        return f"{price:.2f} €".replace(".", ",")
    if isinstance(price, str):
        return price if "€" in price else f"{price} €"
    if isinstance(price, dict):
        for k in ("formatted", "display", "displayValue"):
            if price.get(k):
                return str(price[k])
        amt = price.get("amount") or price.get("value") or price.get("current")
        if isinstance(amt, dict):
            return _price_str(amt)
        if amt is not None:
            try:
                return f"{float(amt):.2f} €".replace(".", ",")
            except (TypeError, ValueError):
                return str(amt)
    return ""


def _loads(s):
    try:
        return json.loads(s) if s else None
    except (json.JSONDecodeError, TypeError):
        return None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _iter_next_data_objects(soup: BeautifulSoup):
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag:
        yield from _walk(_loads(tag.string or tag.get_text()))


# --------------------------------------------------------------------------
# News-Feeds (Vorankündigungen, oft vor dem eigentlichen Drop)
# --------------------------------------------------------------------------
@dataclass
class FeedItem:
    guid: str
    title: str
    link: str
    published: str


class RedditAuth:
    """Offizielle Reddit-API (App-only OAuth). Ohne Anmeldung blockt Reddit RSS-Abrufe oft mit 429."""
    UA = "homeassistant:pokemon-center-monitor:1.0 (personal drop alerts)"

    def __init__(self, client_id: str, client_secret: str):
        self.cid, self.secret = client_id, client_secret
        self.token, self.expires = "", 0.0

    def get_token(self, session: requests.Session) -> str:
        import time
        if self.token and time.time() < self.expires - 60:
            return self.token
        r = session.post("https://www.reddit.com/api/v1/access_token",
                         auth=(self.cid, self.secret), data={"grant_type": "client_credentials"},
                         headers={"User-Agent": self.UA}, timeout=20)
        r.raise_for_status()
        j = r.json()
        self.token, self.expires = j["access_token"], time.time() + float(j.get("expires_in", 3600))
        return self.token


REDDIT: RedditAuth | None = None


def _reddit_items(session: requests.Session, url: str) -> list[FeedItem]:
    m = re.search(r"reddit\.com/r/([^/]+)/(new|hot|rising)?", url)
    if not m:
        raise ValueError(f"Reddit-URL nicht erkannt: {url}")
    sub, listing = m.group(1), m.group(2) or "new"
    r = session.get(f"https://oauth.reddit.com/r/{sub}/{listing}", params={"limit": 25, "raw_json": 1},
                    headers={"Authorization": f"Bearer {REDDIT.get_token(session)}", "User-Agent": RedditAuth.UA},
                    timeout=20)
    r.raise_for_status()
    out = []
    for c in r.json().get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append(FeedItem(d.get("name", ""), d.get("title", ""),
                            "https://www.reddit.com" + d.get("permalink", ""), ""))
    return out


def fetch_feed(session: requests.Session, url: str) -> list[FeedItem]:
    if REDDIT and "reddit.com/r/" in url:
        return _reddit_items(session, url)
    r = session.get(url, timeout=20, headers={
        "User-Agent": UA, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9"})
    r.raise_for_status()
    return parse_feed(r.content)


def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    root = ET.fromstring(xml_bytes)
    items = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        pub = it.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub).astimezone().strftime("%d.%m. %H:%M") if pub else ""
        except (TypeError, ValueError):
            pass
        items.append(FeedItem(guid, title, link, pub))
    for it in root.iter(f"{ns_atom}entry"):
        title = (it.findtext(f"{ns_atom}title") or "").strip()
        link_el = it.find(f"{ns_atom}link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (it.findtext(f"{ns_atom}id") or link or title).strip()
        items.append(FeedItem(guid, title, link, it.findtext(f"{ns_atom}updated") or ""))
    return items
