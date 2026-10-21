"""Zustandsspeicher (SQLite): welche Produkte bekannt sind und in welchem Zustand."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Product:
    url: str
    title: str = ""
    price: str = ""
    availability: str = ""   # "in_stock" | "out_of_stock" | "coming_soon" | "unknown"
    image: str = ""
    sku: str = ""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS products (
                   url TEXT PRIMARY KEY,
                   title TEXT, price TEXT, availability TEXT, image TEXT, sku TEXT,
                   first_seen REAL, last_seen REAL, last_checked REAL
               )"""
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    # --- Produkte -----------------------------------------------------------
    def get(self, url: str) -> Product | None:
        row = self.db.execute(
            "SELECT url,title,price,availability,image,sku FROM products WHERE url=?", (url,)
        ).fetchone()
        return Product(*row) if row else None

    def known_urls(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT url FROM products")}

    def upsert(self, p: Product, checked: bool = False) -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO products(url,title,price,availability,image,sku,first_seen,last_seen,last_checked)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                   title=COALESCE(NULLIF(excluded.title,''),title),
                   price=COALESCE(NULLIF(excluded.price,''),price),
                   availability=COALESCE(NULLIF(excluded.availability,''),availability),
                   image=COALESCE(NULLIF(excluded.image,''),image),
                   sku=COALESCE(NULLIF(excluded.sku,''),sku),
                   last_seen=excluded.last_seen,
                   last_checked=CASE WHEN ? THEN excluded.last_checked ELSE last_checked END""",
            (p.url, p.title, p.price, p.availability, p.image, p.sku, now, now,
             now if checked else None, 1 if checked else 0),
        )
        self.db.commit()

    def due_for_check(self, max_age_s: float, limit: int) -> list[str]:
        """Produkte, deren letzter Detail-Check am längsten her ist (nicht verfügbare zuerst)."""
        cutoff = time.time() - max_age_s
        rows = self.db.execute(
            """SELECT url FROM products
               WHERE last_checked IS NULL OR last_checked < ?
               ORDER BY CASE availability WHEN 'in_stock' THEN 1 ELSE 0 END,
                        COALESCE(last_checked, 0) ASC
               LIMIT ?""",
            (cutoff, limit),
        )
        return [r[0] for r in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    # --- Meta ---------------------------------------------------------------
    def meta_get(self, k: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def meta_set(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()
