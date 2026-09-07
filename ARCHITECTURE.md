# Girl News Pipeline — v2 Architecture

Implementation spec. v1 (flat top-10 list, auto-send via Buttondown) is built and
working; this replaces the classification and delivery layers with a sectioned
editorial digest and a manual send.

---

## 1. What changes from v1

| | v1 (built) | v2 (this spec) |
|---|---|---|
| Sources | 3 RSS feeds | ~11 RSS feeds incl. topic-targeted queries + PubMed/WHO |
| Output | Flat list, top 10 by recency | 4 editorial sections |
| Assault stories | Individual bullets | Geographic roundup ("cases in UP, Delhi, Karnataka…") |
| Classification | Hardcoded `LIKE` chains in SQL | Weighted keyword scoring, keywords stay in YAML |
| Delivery | Auto-send via API | Markdown file, pasted into EmailOctopus by hand |
| LLM | None | Local Ollama for case briefs + long-text summaries |

**Kept as-is:** DuckDB warehouse, dbt transform layer, YAML config, cron trigger,
`ingest/fetch_rss.py` (only its source list changes).

---

## 2. Locked decisions

1. **Local Ollama, not a hosted API.** Model configurable; default `llama3.2:3b`
   (pulled and validated — see §6.7 for the measurements and the prompt-framing
   trap). Must degrade gracefully to rules-based output when Ollama is
   unreachable — the pipeline never hard-fails because the LLM is down.
2. **Section scoring lives in dbt.** Python handles location extraction, LLM
   enrichment, and rendering. Both layers do real work.
3. **RSS only.** No news APIs in v2.
4. **Health & Research is in scope**, with Ollama summarizing dense abstracts.
5. **Manual send.** Pipeline's final artifact is a `.md` file. No email code.

---

## 3. Target structure

```
girl-news-pipeline/
├── ARCHITECTURE.md            ← this file
├── README.md                  ← update: new setup + run instructions
├── requirements.txt
├── .env.example
├── run_pipeline.py            ← rewritten: 5 stages
├── config/
│   ├── sources.yaml           ← rewritten (partially done, needs validation)
│   ├── keywords.yaml          ← rewritten: weighted, per-section
│   ├── exclusions.yaml        ← new: global + per-source exclude rules
│   └── india_locations.yaml   ← new: states, UTs, cities, aliases
├── ingest/
│   ├── fetch_rss.py           ← keep logic, add section_hint column
│   └── load_reference.py      ← new: YAML → DuckDB reference tables
├── dbt/
│   └── models/
│       ├── staging/
│       │   └── stg_articles.sql        ← extend: strip datelines
│       └── marts/
│           └── fct_scored_articles.sql ← new (replaces fct_weekly_digest)
├── transform/
│   ├── __init__.py
│   ├── llm.py                 ← new: Ollama client + fallback
│   ├── locations.py           ← new: dateline-aware location extraction
│   └── enrich.py              ← new: writes enriched_articles
├── deliver/
│   └── build_digest.py        ← new (replaces send_newsletter.py — delete it)
├── output/                    ← new: digest_*.md + digest_*_candidates.md
├── data/warehouse.duckdb
└── logs/
```

Delete `deliver/send_newsletter.py` and `dbt/models/marts/fct_weekly_digest.sql`.

---

## 4. Pipeline stages

```
1. load_reference.py   YAML config      → ref_keywords, ref_exclusions, ref_locations
2. fetch_rss.py        RSS feeds        → raw_articles
3. dbt run             raw_articles     → stg_articles → fct_scored_articles
4. enrich.py           fct_scored       → enriched_articles  (locations + LLM)
5. build_digest.py     enriched         → output/digest_YYYY-MM-DD.md
                                        → output/digest_YYYY-MM-DD_candidates.md
```

Every stage idempotent and independently re-runnable. `run_pipeline.py` takes
`--from-stage N` so a failed LLM step doesn't force a re-fetch.

---

## 5. Data model

**`raw_articles`** — unchanged from v1, plus one column:
```
id, source, title, url, description, published_at, raw_categories, fetched_at,
section_hint TEXT   -- NEW: from sources.yaml, nullable
```

**`ref_keywords`** — loaded from `keywords.yaml` by stage 1:
```
term TEXT, section TEXT, weight INTEGER
```
`section` ∈ `safety | policy | work | health`.

**`ref_exclusions`** — loaded from `exclusions.yaml`:
```
pattern TEXT, field TEXT, source TEXT  -- field ∈ 'title'|'categories'; source NULL = global
```

**`ref_locations`** — loaded from `india_locations.yaml`:
```
name TEXT, canonical TEXT, kind TEXT  -- kind ∈ 'state'|'ut'|'city'
```
`name` is the matchable surface form, `canonical` the display name — so
`Bombay`, `Mumbai` both canonicalize to `Mumbai`; `UP`, `Uttar Pradesh` → `Uttar Pradesh`.

**`enriched_articles`** — written by stage 4:
```
id, section, score, location, location_confidence, brief, summary, enriched_at
```

**`digest_log`** — replaces `sent_digest_log`:
```
digest_date DATE, article_id TEXT, section TEXT
```
Stage 5 writes here. Used both to avoid repeats and to compute "since last digest".

---

## 6. Component specs

### 6.1 `config/keywords.yaml`

Weighted so specific terms outrank generic ones. `women` alone must not be enough
to classify anything — that was the v1 failure that filled the digest with cricket.

```yaml
sections:
  safety:
    weight_threshold: 3
    terms:
      - {term: "gang rape", weight: 5}
      - {term: "sexual assault", weight: 5}
      - {term: "domestic violence", weight: 5}
      - {term: "acid attack", weight: 5}
      - {term: "dowry death", weight: 5}
      - {term: "molestation", weight: 4}
      - {term: "trafficking", weight: 4}
      - {term: "marital rape", weight: 4}
      - {term: "rape", weight: 3}
      - {term: "assault", weight: 2}
      - {term: "harassment", weight: 2}
  policy:
    weight_threshold: 3
    terms:
      - {term: "supreme court", weight: 4}
      - {term: "high court", weight: 4}
      - {term: "ncrb", weight: 5}
      - {term: "verdict", weight: 3}
      - {term: "conviction", weight: 3}
      # …
  work: { … }
  health: { … }
```

Starter term lists are in the v1 `keywords.yaml`; re-bucket them by section and
assign weights. Expect to tune weekly — that's why they're config, not code.

### 6.2 `config/india_locations.yaml`

All 28 states + 8 UTs + ~60 major cities, each with aliases. Include the
collision cases explicitly so they can be handled:

```yaml
locations:
  - {canonical: "Uttar Pradesh", kind: state, aliases: ["Uttar Pradesh", "UP"]}
  - {canonical: "Delhi", kind: ut, aliases: ["Delhi", "New Delhi", "NCR"]}
  - {canonical: "Mumbai", kind: city, aliases: ["Mumbai", "Bombay"]}
  # …
```

### 6.3 `ingest/load_reference.py`

Reads the three YAML configs, truncates and rewrites `ref_keywords`,
`ref_exclusions`, `ref_locations`. Trivial but must run before dbt.

### 6.4 `dbt/models/staging/stg_articles.sql`

Extend the existing model with **dateline stripping**. Indian wire copy opens
`NEW DELHI:` or `MUMBAI, Sept 3:` — that's the reporter's desk, not the incident,
and it is the single biggest source of wrong locations.

Add a `body_text` column = description with any leading
`^[A-Z][A-Z\s]{2,25}[,:]` prefix removed. Keep the original `description` too;
location extraction reads `body_text`, rendering reads `description`.

### 6.5 `dbt/models/marts/fct_scored_articles.sql`

The core of the v2 transform. Joins articles against `ref_keywords` and sums
weights per section — so keywords stay in YAML and SQL stays generic.

```sql
with matched as (
    select a.id, k.section, sum(k.weight) as score
    from {{ ref('stg_articles') }} a
    join ref_keywords k
      on position(k.term in lower(a.title || ' ' || coalesce(a.body_text, ''))) > 0
    group by 1, 2
),
scored as (
    select
        id,
        max(case when section = 'safety' then score else 0 end) as safety_score,
        max(case when section = 'policy' then score else 0 end) as policy_score,
        max(case when section = 'work'   then score else 0 end) as work_score,
        max(case when section = 'health' then score else 0 end) as health_score
    from matched group by 1
)
-- primary_section = argmax over the four scores, NULL if all below threshold
-- + section_hint from source acts as a tiebreaker, never an override
```

Requirements:
- Emit **all four scores**, not just the winner — a story can be both safety and
  policy, and `build_digest` may want the secondary tag.
- `primary_section` is NULL when every score is below threshold. Those rows still
  flow through to the candidates file rather than being dropped.
- Apply `ref_exclusions` as a `is_excluded` boolean column, not a filter — again,
  so excluded items remain visible in candidates for review.
- Window: articles published since the last row in `digest_log`, falling back to
  7 days if the table is empty. **Not** a hardcoded 7-day interval.

### 6.6 `transform/locations.py`

```python
def extract_location(title: str, body_text: str, locations: list[dict])
    -> tuple[str | None, float]:
    """Returns (canonical_name, confidence 0.0-1.0)."""
```

Rules, in order:
1. Match on `body_text` (dateline already stripped by dbt), then title.
2. **Word-boundary regex only** — `\bDelhi\b`. Substring matching produces
   "Bihar" inside "Biharsharif" and "Goa" inside "Goalpara".
3. Reject known false-friend contexts: a city name immediately followed by a
   team/tournament word (`Capitals`, `Indians`, `Super Kings`, `XI`) is sports,
   not a location. Keep this list in `india_locations.yaml` under `false_friends`.
4. City match beats state match (more specific). Multiple distinct states → return
   `None` with low confidence rather than picking arbitrarily.
5. Confidence: 1.0 city in title · 0.8 city in body · 0.6 state in title ·
   0.4 state in body · 0.0 none.

Below 0.5 confidence, the article lands in an **"Other / location unclear"** bucket
in the roundup. Never guess.

### 6.7 `transform/llm.py`

Thin Ollama wrapper. Endpoint `http://localhost:11434/api/generate`.

```python
def is_available() -> bool          # GET /api/tags, 2s timeout
def complete(prompt, max_tokens=40, temperature=0.1) -> str | None
def brief(headline: str) -> str | None      # 2-4 word incident phrase
def summarize(text: str, sentences=2) -> str | None
```

Non-negotiable behaviours:
- **Never raises.** Returns `None` on any failure; callers fall back.
- Model name from `OLLAMA_MODEL` env var, default `llama3.2:3b`.
- Cache results in `enriched_articles` keyed by article id — re-running stage 4
  must not re-prompt for articles already enriched.
- Log every prompt/response at DEBUG so bad output is diagnosable.

#### Model selection — measured on this machine, not guessed

Six real headlines, scored on whether output was usable in the roundup:

| Model | Score | Latency | Notes |
|---|---|---|---|
| `llama3.2:3b` | **6/6** | 3.4s | Cleanest formatting. **Recommended.** |
| `qwen2.5:3b` | 6/6 | 3.2s | Good, but emits hyphens (`women-journalists-beaten`) and leaks locations despite instruction |
| `qwen2.5:0.5b` | 2/6 | 2.5s | Unusable — see below |

Both 3B models are fine; `llama3.2:3b` is the default. Both are pulled already.

#### ⚠️ Three findings that will cost you hours if rediscovered

**1. Prompt framing causes total refusal — and this is the big one.**
`llama3.2:3b` refused *all four* test headlines when the prompt said it was
labeling "crimes against women in India":

> `I cannot provide a response that may promote or glorify violence...`

The identical task, reframed as neutral archival indexing, scored 6/6. **Never
mention crime, violence, rape, or assault in the system framing.** Describe the
job as filing/indexing a news archive. The headline text itself is fine — it's
the instruction framing that trips the refusal.

Interestingly `qwen2.5:0.5b` never refused, so this is model-family-specific. If
you ever need to change the prompt, re-test for refusals before shipping.

**2. Stop tokens are mandatory.** Without them the model answers correctly and
then keeps going, inventing new headlines:
`'dalit woman found dead\n\nHeadline: Two killed as scaffolding collapses...'`
Pass `"stop": ["\n", "Headline:", "Label:"]` and `num_predict: 16`.

**3. Format validation does not catch semantic failure.** `qwen2.5:0.5b` passed
5/6 format checks while producing `gamer girl abduction` for the bus rape case and
`gulf news` for a sports headline — it was echoing source names. Validation
catches shape, not correctness; that's exactly why the model floor is 3B rather
than "whatever passes validation".

#### Validated prompt — use this verbatim

```python
PROMPT = """You are indexing a news archive. For each headline, output a short
topic label of 2-4 words that an archivist would use to file it. Output only the
label, lowercase, no punctuation, no explanation.

Headline: Two killed as scaffolding collapses at Chennai metro site
Label: chennai metro collapse

Headline: Woman journalists allege they were beaten by police during protest
Label: journalists beaten by police

Headline: Supreme Court upholds conviction in 2013 Tarun Tejpal case
Label: tejpal conviction upheld

Headline: {headline}
Label:"""

OPTIONS = {"temperature": 0.1, "num_predict": 16,
           "stop": ["\n", "Headline:", "Label:"]}
```

Validation before trusting output: reject if empty, >6 words, or containing any
refusal marker (`i cannot`, `i can't`, `i'm unable`, `as an ai`). On rejection,
fall back to rules.

Rules fallback for `brief`: regex the incident type from a small pattern list
(`gang rape`, `found dead`, `beaten`, `acid attack`, `set on fire`…) and return
that phrase. Mediocre but never wrong-looking, and it keeps the pipeline running
when Ollama is off.

**Budget:** ~3.5s/call × ~15 safety articles ≈ 1 minute per weekly run.

### 6.8 `transform/enrich.py`

For each row in `fct_scored_articles` not already in `enriched_articles`:
- extract location (safety section only — other sections don't need it)
- generate `brief` via LLM (safety only)
- generate `summary` via LLM when `len(description) > 400` (mainly PubMed abstracts)
- write to `enriched_articles`

### 6.9 `deliver/build_digest.py`

Writes two files.

**`output/digest_YYYY-MM-DD.md`** — the newsletter:

```markdown
# Girl News — Week of September 7, 2026

## Safety & Assault (India)

This week we tracked reported cases in: **Uttar Pradesh** (sleeper bus gang rape),
**Delhi** (journalists beaten by police), **Karnataka** (Dalit woman found dead in
well), **West Bengal** (woman found dead near Bihar border).

- [Full headline of the most significant case](url) — *The Indian Express*
- [Any major verdict or policy response](url) — *The Hindu*

## Law, Policy & Data

- **[Headline](url)** — one-line description. *Source*
- …

## Work & Money (India)

- **[Headline](url)** — one-line description. *Source*

## Health & Research

- **[Study title](url)** — two-sentence Ollama summary. *PubMed*

---
*Sources this week: The Hindu, Indian Express, The Wire, Scroll, Feminism in India,
SheThePeople, PubMed, WHO.*
```

Rules:
- Roundup line groups by canonical location, `location_confidence >= 0.5`.
  Low-confidence items go to a trailing "and elsewhere" clause.
- **Render a section only if it has ≥2 items.** Work & Money and Health will be
  thin most weeks; an empty section looks worse than an absent one.
- Cap: safety roundup unlimited (it's one line) + 2 detail bullets; other
  sections 4–6 bullets each.
- Exclude anything already in `digest_log`.

**`output/digest_YYYY-MM-DD_candidates.md`** — the review file. Everything that
was fetched but *not* used, with its four scores and the reason it was cut
(below threshold / excluded by rule / already sent). Since the send is manual,
this is what lets you pull a story back in instead of trusting the filter blind.

### 6.10 `run_pipeline.py`

Sequences the five stages, `--from-stage N` to resume, writes a JSON run log with
per-stage counts and timings. Keep the existing logging and log-file conventions.

---

## 7. Known gotchas (hit and solved in v1 — don't rediscover these)

1. **DuckDB has no `changes()`.** That's SQLite. For upserts, `SELECT` first then
   `INSERT`, and count in Python.
2. **dbt 1.8 wants `model-paths`**, not `model-path`. Wrong key gives a confusing
   "Additional properties are not allowed" error.
3. **`.env` must be BOM-free.** Notepad saves UTF-8 *with* BOM, which makes
   `python-dotenv` read the first key as `﻿KEY` — `os.getenv("KEY")` then
   returns `None` while `load_dotenv()` still returns `True`. Cost an hour.
   Write it with `UTF8Encoding($false)`.
4. **Pass dbt an absolute `DB_PATH`.** Relative paths resolve against the dbt
   working directory, not the project root.
5. **Never read config at module import time** — read env vars inside functions,
   or `load_dotenv` ordering bites you.
6. **Google News URLs are ~400-char redirect blobs.** Two consequences: dedup must
   key on normalized title, not URL hash (same story via Google vs direct gets
   different hashes); and consider resolving them to the destination URL before
   rendering.
7. **`dbt/profiles.yml` stays in the repo** and is passed via `--profiles-dir`, so
   nothing depends on `~/.dbt/`.

---

## 8. Must verify before relying on it

The feed URLs in `config/sources.yaml` are **partially unvalidated**. Confirmed
working: `feminisminindia.com/feed/`, `shethepeople.tv/rss`, Google News query
feeds. Unverified guesses: The Hindu, Indian Express, The Wire, Scroll, PubMed,
WHO.

**First implementation task:** write a throwaway script that fetches every URL in
`sources.yaml`, reports HTTP status and entry count, and drop or fix whatever
fails. Do this before building anything downstream.

---

## 9. Out of scope for v2

Instagram posting · ML/embedding classification (weighted keywords first — get
the editorial structure right before adding a model) · Airflow · Streamlit
dashboard · auto-send · news APIs.
