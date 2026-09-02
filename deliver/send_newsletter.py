"""
Build the weekly digest and send it via Buttondown.
Reads fct_weekly_digest from DuckDB, deduplicates cross-source near-duplicates
with rapidfuzz, builds Markdown email body, then POSTs to Buttondown API.

Idempotent: if an issue with this week's subject line already exists in Buttondown,
the script logs and exits without creating a duplicate.
"""

import logging
import os
from datetime import date
from pathlib import Path

import duckdb
import requests
import yaml
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_CONFIG = PROJECT_ROOT / "config" / "keywords.yaml"
DB_PATH = Path(os.getenv("DB_PATH", "data/warehouse.duckdb"))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH

BUTTONDOWN_API_BASE = "https://api.buttondown.email/v1"


def load_config() -> dict:
    with open(KEYWORDS_CONFIG) as f:
        return yaml.safe_load(f)


def get_digest_articles() -> list[dict]:
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    rows = conn.execute("""
        SELECT id, source, title, url, description, published_at, digest_rank
        FROM fct_weekly_digest
        WHERE included_in_digest = true
        ORDER BY digest_rank ASC
    """).fetchall()
    conn.close()
    cols = ["id", "source", "title", "url", "description", "published_at", "digest_rank"]
    return [dict(zip(cols, row)) for row in rows]


def fuzzy_dedup(articles: list[dict], threshold: int) -> list[dict]:
    """Remove near-duplicate titles, keeping the first occurrence."""
    kept = []
    for article in articles:
        title = article["title"]
        is_dupe = any(
            fuzz.token_sort_ratio(title, k["title"]) >= threshold
            for k in kept
        )
        if not is_dupe:
            kept.append(article)
    return kept


def format_description(desc: str, max_chars: int = 200) -> str:
    if not desc:
        return ""
    # strip basic HTML tags
    import re
    clean = re.sub(r"<[^>]+>", "", desc).strip()
    clean = " ".join(clean.split())
    return clean[:max_chars] + ("…" if len(clean) > max_chars else "")


def build_email_body(articles: list[dict], week_label: str) -> str:
    source_labels = {
        "feminism_in_india": "Feminism in India",
        "shethepeople": "SheThePeople.TV",
        "google_news": "Google News",
    }
    lines = [
        f"# Girl News — Week of {week_label}",
        "",
        "A weekly digest of India-focused women's news and gender-issue coverage.",
        "",
        "---",
        "",
    ]
    for a in articles:
        source = source_labels.get(a["source"], a["source"])
        desc = format_description(a.get("description", ""))
        lines.append(f"### [{a['title']}]({a['url']})")
        lines.append(f"*{source}*")
        if desc:
            lines.append(f"")
            lines.append(desc)
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("*You're receiving this because you subscribed to Girl News on Buttondown.*")
    return "\n".join(lines)


def send_via_buttondown(subject: str, body: str) -> dict:
    BUTTONDOWN_API_KEY = os.getenv("BUTTONDOWN_API_KEY")
    if not BUTTONDOWN_API_KEY:
        raise RuntimeError("BUTTONDOWN_API_KEY not set in environment")

    headers = {
        "Authorization": f"Token {BUTTONDOWN_API_KEY}",
        "Content-Type": "application/json",
        "X-Buttondown-Live-Dangerously": "true",
    }

    # Check for existing email with same subject (idempotency guard)
    check_resp = requests.get(
        f"{BUTTONDOWN_API_BASE}/emails",
        headers=headers,
        params={"subject": subject},
        timeout=15,
    )
    check_resp.raise_for_status()
    existing = check_resp.json().get("results", [])
    if existing:
        logger.info(f"Email with subject '{subject}' already exists — skipping send")
        return {"status": "already_sent", "id": existing[0]["id"]}

    payload = {
        "subject": subject,
        "body": body,
        "status": "about_to_send",
    }
    resp = requests.post(
        f"{BUTTONDOWN_API_BASE}/emails",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"Buttondown API error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def log_sent_articles(articles: list[dict], digest_week: date) -> None:
    from datetime import datetime, timezone
    conn = duckdb.connect(str(DB_PATH))
    now = datetime.now(timezone.utc).isoformat()
    for a in articles:
        conn.execute("""
            INSERT INTO sent_digest_log (article_id, digest_week, sent_at)
            VALUES (?, ?, ?)
            ON CONFLICT (article_id) DO NOTHING
        """, [a["id"], digest_week.isoformat(), now])
    conn.close()
    logger.info(f"Logged {len(articles)} articles to sent_digest_log")


def run() -> dict:
    config = load_config()
    threshold = config.get("dedup_fuzzy_threshold", 90)

    articles = get_digest_articles()
    if not articles:
        logger.warning("No articles in fct_weekly_digest marked included_in_digest=true")
        return {"status": "skipped", "reason": "no articles"}

    articles = fuzzy_dedup(articles, threshold)
    logger.info(f"After fuzzy dedup: {len(articles)} articles")

    today = date.today()
    week_label = today.strftime("%B %d, %Y")
    subject = f"Girl News — Week of {week_label}"
    body = build_email_body(articles, week_label)

    result = send_via_buttondown(subject, body)
    logger.info(f"Buttondown result: {result}")

    if result.get("status") != "already_sent":
        log_sent_articles(articles, today)

    return {"status": "sent", "articles": len(articles), "buttondown": result}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(result)
