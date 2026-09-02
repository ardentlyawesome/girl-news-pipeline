-- Selects the top N articles for the current week's digest.
-- Applies keyword relevance filter, exclude rules, and skips articles
-- already logged in sent_digest_log (written by deliver/send_newsletter.py).
--
-- Fuzzy cross-source dedup is handled in Python (rapidfuzz) at deliver time.

{{
  config(
    materialized = 'table',
    unique_key = 'id'
  )
}}

with staged as (
    select * from {{ ref('stg_articles') }}
),

recent as (
    select *
    from staged
    where published_at >= current_timestamp - interval '7 days'
),

keyword_filtered as (
    select *
    from recent
    where
        lower(title || ' ' || coalesce(description, '')) like '%women%'
        or lower(title || ' ' || coalesce(description, '')) like '%gender%'
        or lower(title || ' ' || coalesce(description, '')) like '%dalit women%'
        or lower(title || ' ' || coalesce(description, '')) like '%sexual harassment%'
        or lower(title || ' ' || coalesce(description, '')) like '%domestic violence%'
        or lower(title || ' ' || coalesce(description, '')) like '%women''s rights%'
        or lower(title || ' ' || coalesce(description, '')) like '%maternity%'
        or lower(title || ' ' || coalesce(description, '')) like '%menstrual%'
        or lower(title || ' ' || coalesce(description, '')) like '%women safety%'
        or lower(title || ' ' || coalesce(description, '')) like '%rape case%'
        or lower(title || ' ' || coalesce(description, '')) like '%dowry%'
        or lower(title || ' ' || coalesce(description, '')) like '%women farmers%'
        or lower(title || ' ' || coalesce(description, '')) like '%girl child%'
        or lower(title || ' ' || coalesce(description, '')) like '%women entrepreneurs%'
        or lower(title || ' ' || coalesce(description, '')) like '%pay gap%'
        or lower(title || ' ' || coalesce(description, '')) like '%reservation women%'
        or lower(title || ' ' || coalesce(description, '')) like '%feminist%'
        or lower(title || ' ' || coalesce(description, '')) like '%feminism%'
        or lower(title || ' ' || coalesce(description, '')) like '%gender equality%'
        or lower(title || ' ' || coalesce(description, '')) like '%girl education%'
        or lower(title || ' ' || coalesce(description, '')) like '%women in politics%'
        or lower(title || ' ' || coalesce(description, '')) like '%trafficking%'
        or lower(title || ' ' || coalesce(description, '')) like '%acid attack%'
        or lower(title || ' ' || coalesce(description, '')) like '%maternal health%'
        or lower(title || ' ' || coalesce(description, '')) like '%child marriage%'
),

category_excluded as (
    select *
    from keyword_filtered
    where not (
        source = 'shethepeople'
        and (
            lower(coalesce(raw_categories, '')) like '%web stories%'
            or lower(coalesce(raw_categories, '')) like '%entertainment%'
            or lower(coalesce(raw_categories, '')) like '%celebrity%'
            or lower(coalesce(raw_categories, '')) like '%lifestyle%'
            or lower(coalesce(raw_categories, '')) like '%beauty%'
            or lower(coalesce(raw_categories, '')) like '%fashion%'
            or lower(coalesce(raw_categories, '')) like '%horoscope%'
        )
    )
    and not (
        lower(title) like '%film review%'
        or lower(title) like '%movie review%'
        or lower(title) like '%web series review%'
        or lower(title) like '%ott review%'
        or lower(title) like '%recipe%'
        or lower(title) like '%horoscope%'
        -- sports results from google_news
        or lower(title) like '%cricket%'
        or lower(title) like '%asia cup%'
        or lower(title) like '%champions trophy%'
        or lower(title) like '%live streaming%'
        or lower(title) like '%match preview%'
        or lower(title) like '%mutual funds%'
    )
),

-- sent_digest_log is created by ingest/fetch_rss.py and written by
-- deliver/send_newsletter.py after each successful send.
already_sent as (
    select article_id as id
    from sent_digest_log
),

unsent as (
    select c.*
    from category_excluded c
    left join already_sent s on c.id = s.id
    where s.id is null
),

-- Rank within each source first (recency), cap at 4 per source so no single
-- source dominates the digest. Then pick the global top 10.
ranked_per_source as (
    select
        *,
        row_number() over (partition by source order by published_at desc) as source_rank
    from unsent
),

capped as (
    select * from ranked_per_source where source_rank <= 4
),

ranked as (
    select
        *,
        row_number() over (order by published_at desc) as rank
    from capped
)

select
    id,
    source,
    title,
    url,
    description,
    published_at,
    raw_categories,
    fetched_at,
    date_trunc('week', current_date)::date  as digest_week,
    (rank <= 10)                             as included_in_digest,
    rank                                     as digest_rank
from ranked
