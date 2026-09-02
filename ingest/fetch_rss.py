"""
Fetch articles from all configured RSS sources and write them to raw_articles in DuckDB.
Idempotent — safe to run multiple times; new rows are upserted by url hash.
"""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import feedparser
import yaml
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_CONFIG = PROJECT_ROOT / "config" / "sources.yaml"
DB_PATH = Path(os.getenv("DB_PATH", "data/warehouse.duckdb"))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH


def _article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _parse_published(entry) -> str:
    """Return ISO timestamp from feedparser entry, falling back to now."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _get_description(entry) -> str:
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list):
                val = val[0].get("value", "")
            return str(val)[:2000]
    return ""


def _get_categories(entry) -> str:
    tags = getattr(entry, "tags", [])
    return ", ".join(t.get("term", "") for t in tags if t.get("term"))


def ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_articles (
            id              TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            title           TEXT,
            url             TEXT,
            description     TEXT,
            published_at    TIMESTAMPTZ,
            raw_categories  TEXT,
            fetched_at      TIMESTAMPTZ
        )
    """)
    # Tracks articles that have been successfully sent; read by fct_weekly_digest.sql
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_digest_log (
            article_id  TEXT PRIMARY KEY,
            digest_week DATE,
            sent_at     TIMESTAMPTZ
        )
    """)


def fetch_source(source: dict) -> list[dict]:
    name = source["name"]
    url = source["url"]
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feedparser bozo error: {feed.bozo_exception}")
        articles = []
        now = datetime.now(timezone.utc).isoformat()
        for entry in feed.entries:
            link = getattr(entry, "link", "")
            if not link or not link.startswith("http"):
                continue
            articles.append({
                "id": _article_id(link),
                "source": name,
                "title": getattr(entry, "title", "").strip(),
                "url": link,
                "description": _get_description(entry),
                "published_at": _parse_published(entry),
                "raw_categories": _get_categories(entry),
                "fetched_at": now,
            })
        logger.info(f"{name}: fetched {len(articles)} articles")
        return articles
    except Exception as exc:
        logger.error(f"{name}: fetch failed — {exc}")
        return []


def upsert_articles(conn: duckdb.DuckDBPyConnection, articles: list[dict]) -> int:
    if not articles:
        return 0
    inserted = 0
    for a in articles:
        existing = conn.execute(
            "SELECT 1 FROM raw_articles WHERE id = ?", [a["id"]]
        ).fetchone()
        if existing:
            continue
        try:
            conn.execute("""
                INSERT INTO raw_articles
                    (id, source, title, url, description, published_at, raw_categories, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                a["id"], a["source"], a["title"], a["url"],
                a["description"], a["published_at"], a["raw_categories"], a["fetched_at"]
            ])
            inserted += 1
        except Exception as exc:
            logger.warning(f"Failed to insert {a.get('url')}: {exc}")
    return inserted


def run() -> dict:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SOURCES_CONFIG) as f:
        config = yaml.safe_load(f)

    conn = duckdb.connect(str(DB_PATH))
    ensure_table(conn)

    total_fetched = 0
    total_inserted = 0

    for source in config["sources"]:
        articles = fetch_source(source)
        total_fetched += len(articles)
        inserted = upsert_articles(conn, articles)
        total_inserted += inserted

    conn.close()
    logger.info(f"Ingest complete: {total_fetched} fetched, {total_inserted} new rows")
    return {"fetched": total_fetched, "inserted": total_inserted}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(result)
