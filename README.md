# Girl News Pipeline

Automated weekly digest of India-focused women's news and gender-issue coverage.
Pulls from 3 RSS sources, filters for relevance, deduplicates, and sends a curated
email via Buttondown every Sunday.

**Stack:** Python · feedparser · DuckDB · dbt · rapidfuzz · Buttondown API · cron

---

## Architecture

```
RSS feeds (3 sources)
       ↓
ingest/fetch_rss.py  →  DuckDB raw_articles
       ↓
dbt run  →  stg_articles (staging view)  →  fct_weekly_digest (mart table)
       ↓
deliver/send_newsletter.py  →  Buttondown API  →  email sent
       ↑
run_pipeline.py (orchestrator, triggered by cron weekly)
```

---

## Setup (new machine)

### 1. Clone and install

```bash
git clone <repo-url>
cd girl-news-pipeline
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

```bash
cp .env.example .env
# Edit .env and set:
#   BUTTONDOWN_API_KEY=<your key from buttondown.email/settings/api>
#   DB_PATH=data/warehouse.duckdb   (default, leave as-is usually)
```

### 3. dbt profile

The `dbt/profiles.yml` file is already committed and reads `DB_PATH` from the
environment. **Do not move it** — `run_pipeline.py` passes `--profiles-dir dbt/`
to dbt so the profile resolves correctly without modifying `~/.dbt/profiles.yml`.

### 4. First run (manual test)

```bash
# Ingest only (no Buttondown needed yet)
python -m ingest.fetch_rss

# Transform only
cd dbt && dbt run --profiles-dir . && cd ..

# Full pipeline, skipping email send
python run_pipeline.py --skip-deliver

# Full pipeline including email send (needs BUTTONDOWN_API_KEY)
python run_pipeline.py
```

Check `logs/` for JSON run logs after each run.

---

## Cron setup (Linux/macOS)

Add to crontab (`crontab -e`) to run every Sunday at 8am IST (2:30am UTC):

```
30 2 * * 0 /path/to/.venv/bin/python /path/to/girl-news-pipeline/run_pipeline.py >> /path/to/girl-news-pipeline/logs/cron.log 2>&1
```

On Windows, use Task Scheduler with the same command.

---

## Tuning the pipeline

### Keyword list

Edit `config/keywords.yaml` → `include_keywords`. No code change needed.
The dbt SQL in `fct_weekly_digest.sql` uses these patterns; after editing keywords,
re-run `dbt run` to apply.

> **Note:** currently the SQL patterns are written out explicitly in the dbt model.
> If you update `keywords.yaml`, also update the LIKE clauses in
> `dbt/models/marts/fct_weekly_digest.sql` to match. A v2 improvement would
> generate these dynamically from the YAML via a dbt macro.

### Digest size

`config/keywords.yaml` → `digest_max_articles` (default 10).
The dbt model uses `rank <= 10` — update that constant too.

### Sources

Edit `config/sources.yaml` to add or remove feeds. Each source needs a unique `name`
(used as the identifier in the database) and a `url`.

---

## Project structure

```
girl-news-pipeline/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── run_pipeline.py          # weekly orchestrator
├── config/
│   ├── sources.yaml         # RSS feed URLs
│   └── keywords.yaml        # relevance filter rules + digest settings
├── ingest/
│   └── fetch_rss.py         # pulls feeds → raw_articles in DuckDB
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml
│   └── models/
│       ├── staging/
│       │   └── stg_articles.sql       # dedupe + clean
│       └── marts/
│           └── fct_weekly_digest.sql  # filter + rank + select top N
├── deliver/
│   └── send_newsletter.py   # Buttondown API call
├── data/
│   └── warehouse.duckdb     # gitignored — created on first run
└── logs/                    # gitignored — JSON run logs
```

---

## Buttondown setup (one-time manual step)

1. Create a free account at [buttondown.email](https://buttondown.email)
2. Go to Settings → API → copy your API key
3. Paste it into `.env` as `BUTTONDOWN_API_KEY`

The pipeline creates and sends the email issue automatically each week.
The idempotency guard in `send_newsletter.py` checks if an issue with the same
subject line already exists before creating a new one, so re-runs don't double-send.

---

## v2 Roadmap

- Replace keyword rules with a lightweight TF-IDF classifier
- Instagram posting via the Business/Graph API
- Add BehanBox feed (confirm Substack feed URL first)
- Swap cron → Airflow DAG
- Streamlit dashboard over the DuckDB file
- LLM-generated one-line blurbs per article
