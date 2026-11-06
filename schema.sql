-- Oligo Space pricing bot · Supabase (PostgreSQL) schema
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).

-- ---------------------------------------------------------------------------
-- bus_catalog: one row per competitor bus / platform family (from the research file).
-- Ranges are stored as lo/hi so the optimizer can tell "meets spec" from
-- "needs an upgraded configuration".
-- ---------------------------------------------------------------------------
create table if not exists bus_catalog (
    bus_id         text primary key,
    company        text        not null,
    model          text        not null,
    size_class     text,
    payload_lo     numeric,                 -- kg
    payload_hi     numeric,
    power_lo       numeric,                 -- W, orbit-average
    power_hi       numeric,
    pointing_base  numeric,                 -- deg, standard configuration (larger = looser)
    pointing_best  numeric,                 -- deg, with precision package
    lead_min       numeric,                 -- months
    lead_max       numeric,
    propulsion     text        not null default '',   -- letters: E C G N
    flags          jsonb       not null default '[]', -- family / conflict / development / scope
    form_factors   jsonb       not null default '[]',
    confidence     text,
    updated_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- price_trends: every price observation, scraped or seeded.
-- `state` is the regional tag requested for the scrape ('ALL' when the price
-- is not region dependent). price is the point value in the native currency;
-- price_low / price_high keep published ranges.
-- ---------------------------------------------------------------------------
create table if not exists price_trends (
    id          bigint generated always as identity primary key,
    "timestamp" timestamptz   not null default now(),
    observed_on date          not null default current_date,
    state       text          not null default 'ALL',
    item_name   text          not null,
    price       numeric(16,2) not null check (price > 0),
    currency    char(3)       not null default 'USD',
    price_low   numeric(16,2),
    price_high  numeric(16,2),
    bus_id      text,                                  -- soft reference to bus_catalog
    supplier    text,
    kind        text          not null default 'Bus price',
    price_basis text          not null default 'single', -- single | volume | contract | service | unknown
    quantity    integer,
    nre         text          not null default 'unknown', -- included | excluded | unknown
    confidence  text          not null default 'Published',
    source_url  text          not null default '',
    specs       jsonb         not null default '{}',      -- spec of the exact priced configuration
    -- one observation per item / region / source / day => re-running a job is idempotent
    constraint price_trends_uniq unique (item_name, state, source_url, observed_on)
);

create index if not exists price_trends_time_idx on price_trends ("timestamp" desc);
create index if not exists price_trends_state_idx on price_trends (state);
create index if not exists price_trends_bus_idx on price_trends (bus_id);

-- ---------------------------------------------------------------------------
-- press_releases: newest articles from competitor / agency news pages.
-- ---------------------------------------------------------------------------
create table if not exists press_releases (
    id             bigint generated always as identity primary key,
    date_published date,
    source         text        not null,
    title          text        not null,
    url            text        not null unique,   -- dedupe key ("is this article new?")
    content        text,
    alert_flag     boolean     not null default false,
    alert_keywords text[]      not null default '{}',
    scraped_at     timestamptz not null default now()
);

create index if not exists press_date_idx on press_releases (date_published desc nulls last);
create index if not exists press_alert_idx on press_releases (alert_flag) where alert_flag;

-- ---------------------------------------------------------------------------
-- latest_prices: most recent observation per item / region / source.
-- The optimizer reads this view so old scrapes never skew the benchmark.
-- ---------------------------------------------------------------------------
create or replace view latest_prices as
select distinct on (item_name, state, source_url) *
from price_trends
order by item_name, state, source_url, "timestamp" desc;

-- ---------------------------------------------------------------------------
-- Provenance columns (added after first release; safe to re-run).
-- ---------------------------------------------------------------------------
alter table price_trends   add column if not exists source_title text not null default '';
alter table price_trends   add column if not exists source_type  text not null default '';
alter table press_releases add column if not exists category     text not null default 'Press release';

-- ---------------------------------------------------------------------------
-- Security: Supabase exposes public tables through its REST API using the anon
-- key. Turn on row level security with no policies so only your direct Postgres
-- connection (scraper, Streamlit) can read or write.
-- ---------------------------------------------------------------------------
alter table bus_catalog     enable row level security;
alter table price_trends    enable row level security;
alter table press_releases  enable row level security;
