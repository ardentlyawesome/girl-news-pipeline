"""
Build the weekly digest and send it via EmailOctopus.
Reads fct_weekly_digest from DuckDB, deduplicates with rapidfuzz,
builds an HTML email body, creates a campaign, then sends it.

Idempotent: if a campaign with this week's name already exists,
the script logs and exits without creating a duplicate.
"""

import logging
import os
import re
from datetime import date, datetime, timezone
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

EO_API_BASE = "https://emailoctopus.com/api/1.6"


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


def clean_description(desc: str, max_chars: int = 220) -> str:
    if not desc:
        return ""
    clean = re.sub(r"<[^>]+>", "", desc)
    clean = re.sub(r"&[a-z]+;", " ", clean)
    clean = " ".join(clean.split())
    return clean[:max_chars] + ("…" if len(clean) > max_chars else "")


def build_html_body(articles: list[dict], week_label: str) -> str:
    source_labels = {
        "feminism_in_india": "Feminism in India",
        "shethepeople": "SheThePeople.TV",
        "google_news": "Google News",
    }

    items_html = ""
    for a in articles:
        source = source_labels.get(a["source"], a["source"])
        desc = clean_description(a.get("description", ""))
        desc_html = f"<p style='margin:6px 0 0;color:#444;font-size:14px;'>{desc}</p>" if desc else ""
        items_html += f"""
        <div style='margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e5e5e5;'>
          <p style='margin:0 0 4px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:.05em;'>{source}</p>
          <h2 style='margin:0;font-size:17px;line-height:1.4;'>
            <a href='{a["url"]}' style='color:#1a1a1a;text-decoration:none;'>{a["title"]}</a>
          </h2>
          {desc_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>
<body style='margin:0;padding:0;background:#f7f7f7;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'>
  <table width='100%' cellpadding='0' cellspacing='0' style='background:#f7f7f7;padding:32px 16px;'>
    <tr><td align='center'>
      <table width='600' cellpadding='0' cellspacing='0' style='max-width:600px;width:100%;background:#fff;border-radius:8px;padding:40px;'>
        <tr><td>
          <h1 style='margin:0 0 4px;font-size:24px;color:#1a1a1a;'>Girl News</h1>
          <p style='margin:0 0 32px;color:#888;font-size:14px;'>Week of {week_label} · India women &amp; gender</p>
          <hr style='border:none;border-top:2px solid #1a1a1a;margin-bottom:32px;'>
          {items_html}
          <p style='margin:32px 0 0;font-size:12px;color:#aaa;text-align:center;'>
            You're receiving this because you subscribed to Girl News.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def get_eo_credentials() -> tuple[str, str, str, str]:
    api_key = os.getenv("EMAILOCTOPUS_API_KEY")
    list_id = os.getenv("EMAILOCTOPUS_LIST_ID")
    from_name = os.getenv("FROM_NAME", "Girl News")
    from_email = os.getenv("FROM_EMAIL")
    if not api_key:
        raise RuntimeError("EMAILOCTOPUS_API_KEY not set in environment")
    if not list_id:
        raise RuntimeError("EMAILOCTOPUS_LIST_ID not set in environment")
    if not from_email:
        raise RuntimeError("FROM_EMAIL not set in environment")
    return api_key, list_id, from_name, from_email


def campaign_exists(api_key: str, campaign_name: str) -> str | None:
    """Return existing campaign ID if a campaign with this name exists, else None."""
    resp = requests.get(
        f"{EO_API_BASE}/campaigns",
        params={"api_key": api_key, "limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    for campaign in resp.json().get("data", []):
        if campaign.get("name") == campaign_name:
            return campaign["id"]
    return None


def create_campaign(api_key: str, from_name: str, from_email: str,
                    subject: str, name: str, html_body: str) -> str:
    resp = requests.post(
        f"{EO_API_BASE}/campaigns",
        json={
            "api_key": api_key,
            "name": name,
            "subject": subject,
            "from": {"name": from_name, "email_address": from_email},
            "content": {"html": html_body},
        },
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"EmailOctopus create error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()["id"]


def send_campaign(api_key: str, campaign_id: str, list_id: str) -> dict:
    resp = requests.post(
        f"{EO_API_BASE}/campaigns/{campaign_id}/send",
        json={"api_key": api_key, "list_ids": [list_id]},
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"EmailOctopus send error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def log_sent_articles(articles: list[dict], digest_week: date) -> None:
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

    api_key, list_id, from_name, from_email = get_eo_credentials()

    today = date.today()
    week_label = today.strftime("%B %d, %Y")
    campaign_name = f"Girl News — Week of {week_label}"
    subject = campaign_name
    html_body = build_html_body(articles, week_label)

    # Idempotency: don't create a duplicate campaign for the same week
    existing_id = campaign_exists(api_key, campaign_name)
    if existing_id:
        logger.info(f"Campaign '{campaign_name}' already exists (id={existing_id}) — skipping")
        return {"status": "already_sent", "campaign_id": existing_id}

    campaign_id = create_campaign(api_key, from_name, from_email, subject, campaign_name, html_body)
    logger.info(f"Campaign created: {campaign_id}")

    result = send_campaign(api_key, campaign_id, list_id)
    logger.info(f"Campaign sent: {result}")

    log_sent_articles(articles, today)

    return {"status": "sent", "articles": len(articles), "campaign_id": campaign_id}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(result)
