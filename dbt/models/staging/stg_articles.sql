-- Deduplicate raw_articles, normalize timestamps and text, drop junk rows.
-- Materialized as a view so it always reflects the latest raw data.

with source as (
    select * from raw_articles
),

cleaned as (
    select
        id,
        source,
        trim(title)                                     as title,
        url,
        trim(description)                               as description,
        cast(published_at as timestamptz)               as published_at,
        raw_categories,
        cast(fetched_at as timestamptz)                 as fetched_at,
        -- row_number to keep first-seen record per id
        row_number() over (partition by id order by fetched_at asc) as rn
    from source
    where
        title is not null
        and trim(title) != ''
        and url is not null
        and url like 'http%'
)

select
    id,
    source,
    title,
    url,
    description,
    published_at,
    raw_categories,
    fetched_at
from cleaned
where rn = 1
