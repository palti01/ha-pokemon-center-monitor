"""Benachrichtigungskanäle: Home Assistant (Webhook), ntfy, Telegram."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

log = logging.getLogger("pcmonitor.notify")


@dataclass
class Event:
    kind: str            # "new" | "restock" | "queue" | "price" | "test"
    title: str
    url: str = ""
    price: str = ""
    availability: str = ""
    image: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def headline(self) -> str:
        return {
            "new": "🆕 Neu im Pokémon Center",
            "restock": "✅ Wieder verfügbar",
            "queue": "⏳ Warteschlange aktiv – Drop läuft!",
            "price": "💶 Preis geändert",
            "test": "🧪 Testnachricht",
            "status": "ℹ️ Monitor-Status",
        }.get(self.kind, "Pokémon Center")

    def body(self) -> str:
        parts = [self.title]
        if self.price:
            parts.append(self.price)
        if self.availability:
            parts.append(self.availability)
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "headline": self.headline,
            "title": self.title,
            "message": self.body(),
            "url": self.url,
            "price": self.price,
            "availability": self.availability,
            "image": self.image,
            **self.extra,
        }


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.session = requests.Session()

    def send(self, ev: Event) -> None:
        sent = False
        ha = self.cfg.get("homeassistant") or {}
        if ha.get("webhook_url"):
            sent |= self._post(ha["webhook_url"], json=ev.as_dict(), name="Home Assistant")

        ntfy = self.cfg.get("ntfy") or {}
        if ntfy.get("url"):
            headers = {
                "Title": ev.headline.encode("utf-8"),
                "Priority": "urgent" if ev.kind in ("new", "restock", "queue") else "default",
                "Tags": "pokemon",
            }
            if ev.url:
                headers["Click"] = ev.url
            if ev.image:
                headers["Attach"] = ev.image
            if ntfy.get("token"):
                headers["Authorization"] = f"Bearer {ntfy['token']}"
            sent |= self._post(ntfy["url"], data=ev.body().encode("utf-8"), headers=headers, name="ntfy")

        tg = self.cfg.get("telegram") or {}
        if tg.get("bot_token") and tg.get("chat_id"):
            text = f"<b>{_esc(ev.headline)}</b>\n{_esc(ev.body())}"
            if ev.url:
                text += f'\n<a href="{_esc(ev.url)}">Zum Produkt</a>'
            sent |= self._post(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                json={"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": False},
                name="Telegram",
            )

        if not sent:
            log.warning("Kein Kanal erfolgreich – Event nur im Log: %s | %s", ev.headline, ev.body())
        log.info("EVENT %s: %s %s", ev.kind, ev.body(), ev.url)

    def _post(self, url: str, name: str, **kw) -> bool:
        try:
            r = self.session.post(url, timeout=15, **kw)
            if r.status_code >= 300:
                log.error("%s antwortet %s: %s", name, r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.error("%s nicht erreichbar: %s", name, e)
            return False


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
