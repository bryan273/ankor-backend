-- =============================================================================
-- Anker Care Agent — full schema
--
-- PASTE THIS ONCE into the Supabase SQL editor (Dashboard → SQL Editor → New query).
-- The sb_secret_ API key can read and write ROWS through PostgREST but cannot run
-- DDL, so the tables have to be created either here, or by us over a direct
-- Postgres connection once we have the DB password / a management PAT.
--
-- Idempotent: safe to run again after edits.
-- Vectors live in Pinecone (index `anker-support`, 3072-d cosine); this database
-- keeps the canonical rows and the pinecone_id that points at them.
-- =============================================================================

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ─────────────────────────────────────────────────────────────────────────────
-- Catalog
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists products (
  id            uuid primary key default gen_random_uuid(),
  brand         text not null,                      -- anker | eufy | eufy Baby | soundcore | ...
  sku           text not null unique,
  name          text not null,
  slug          text,
  category      text,                               -- robot_vacuum | breast_pump | charger | ...
  form_factor   text,                               -- matched against VLM `detected.form_factor`
  price         numeric(10,2),
  currency      text default 'USD',
  url           text,
  hero_image    text,
  status        text default 'active',              -- active | discontinued
  warranty_months int,
  raw           jsonb default '{}'::jsonb,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);
create index if not exists products_brand_idx    on products (brand);
create index if not exists products_category_idx on products (category);
create index if not exists products_name_trgm    on products using gin (name gin_trgm_ops);

-- Drives product disambiguation (scenario S2). "s1 pro" MUST resolve to >= 2 rows.
create table if not exists product_aliases (
  id          uuid primary key default gen_random_uuid(),
  alias       text not null,                        -- normalised lowercase
  product_id  uuid not null references products(id) on delete cascade,
  confidence  real default 1.0,
  source      text default 'derived',               -- derived | manual | community
  unique (alias, product_id)
);
create index if not exists product_aliases_alias_idx on product_aliases (alias);

create table if not exists product_specs (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid not null references products(id) on delete cascade,
  key         text not null,
  value       text,
  unit        text,
  unique (product_id, key)
);

create table if not exists product_media (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid not null references products(id) on delete cascade,
  url         text not null,
  kind        text default 'image',                 -- image | video
  ord         int default 0,
  caption     text,                                 -- VLM caption; this is what gets embedded
  vlm_tags    jsonb default '{}'::jsonb,
  unique (product_id, url)
);

create table if not exists product_docs (
  id           uuid primary key default gen_random_uuid(),
  product_id   uuid references products(id) on delete cascade,
  kind         text,                                -- manual | quickstart | datasheet | faq
  title        text,
  url          text not null unique,
  local_path   text,
  parsed_text  text,
  pages        int,
  fetched_at   timestamptz default now()
);

-- Exact lookup beats RAG for "E-05". Populated from manual error tables.
create table if not exists error_codes (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid references products(id) on delete cascade,
  code        text not null,
  meaning     text,
  severity    text default 'normal',                -- normal | safety
  fix_steps   jsonb default '[]'::jsonb,
  source_url  text,
  unique (product_id, code)
);
create index if not exists error_codes_code_idx on error_codes (upper(code));

-- ─────────────────────────────────────────────────────────────────────────────
-- Knowledge base
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists kb_articles (
  id          uuid primary key default gen_random_uuid(),
  source_url  text not null unique,
  title       text,
  doc_type    text,                                 -- manual | faq | troubleshooting | blog | community
  product_ids uuid[] default '{}',
  body        text,
  lang        text default 'en',
  fetched_at  timestamptz default now()
);

create table if not exists kb_chunks (
  id          uuid primary key default gen_random_uuid(),
  article_id  uuid not null references kb_articles(id) on delete cascade,
  ord         int not null,
  text        text not null,
  text_hash   text not null,                        -- re-embed only what changed
  pinecone_id text,
  meta        jsonb default '{}'::jsonb,            -- sku[], section, page, url
  embedded_at timestamptz,
  unique (article_id, ord)
);
create index if not exists kb_chunks_hash_idx on kb_chunks (text_hash);

create table if not exists troubleshooting_flows (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid references products(id) on delete cascade,
  symptom     text not null,
  steps       jsonb not null default '[]'::jsonb,   -- renders as a diagnostic_steps block
  est_minutes int,
  source_url  text
);
create index if not exists ts_flows_symptom_idx on troubleshooting_flows using gin (symptom gin_trgm_ops);

-- ─────────────────────────────────────────────────────────────────────────────
-- Commerce (demo data — every row carries source='demo')
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists customers (
  id        uuid primary key default gen_random_uuid(),
  email     text unique,
  phone     text,
  name      text,
  locale    text default 'en',
  source    text default 'demo',
  created_at timestamptz default now()
);

create table if not exists dealers (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  region          text,
  order_no_pattern text,                            -- e.g. '^SE-\d{6}$' — how we recognise their invoices
  contact         text,
  service_path    text,                             -- what the customer should actually do
  authorized      boolean default true,
  source          text default 'demo'
);

create table if not exists orders (
  id            uuid primary key default gen_random_uuid(),
  order_no      text not null unique,
  customer_id   uuid references customers(id) on delete set null,
  channel       text not null,                      -- official_store | amazon | dealer | unknown
  purchase_date date,
  status        text,
  total         numeric(10,2),
  currency      text default 'USD',
  source        text default 'demo',
  created_at    timestamptz default now()
);

create table if not exists order_items (
  id         uuid primary key default gen_random_uuid(),
  order_id   uuid not null references orders(id) on delete cascade,
  product_id uuid references products(id) on delete set null,
  qty        int default 1,
  serial     text
);

-- Scenario S3: these order numbers exist ONLY here, never in `orders`.
create table if not exists dealer_orders (
  id            uuid primary key default gen_random_uuid(),
  dealer_id     uuid not null references dealers(id) on delete cascade,
  order_no      text not null,
  product_id    uuid references products(id) on delete set null,
  purchase_date date,
  customer_ref  text,
  source        text default 'demo',
  unique (dealer_id, order_no)
);
create index if not exists dealer_orders_no_idx on dealer_orders (order_no);

create table if not exists warranty_policies (
  id        uuid primary key default gen_random_uuid(),
  category  text not null,
  months    int not null,
  covers    jsonb default '[]'::jsonb,
  excludes  jsonb default '[]'::jsonb,
  unique (category)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Conversation
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists sessions (
  id            uuid primary key default gen_random_uuid(),
  customer_id   uuid references customers(id) on delete set null,
  locale        text default 'en',
  resolved_sku  text,
  meta          jsonb default '{}'::jsonb,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create table if not exists messages (
  id          uuid primary key default gen_random_uuid(),
  session_id  uuid not null references sessions(id) on delete cascade,
  role        text not null,                        -- user | assistant | system
  text        text,
  emotion     text,
  intensity   real,
  intent      text,
  urgency     jsonb,
  created_at  timestamptz default now()
);
create index if not exists messages_session_idx on messages (session_id, created_at);

create table if not exists message_blocks (
  id          uuid primary key default gen_random_uuid(),
  message_id  uuid not null references messages(id) on delete cascade,
  block_id    text not null unique,                 -- the id the frontend posts back
  type        text not null,
  payload     jsonb not null default '{}'::jsonb,
  actions     jsonb not null default '[]'::jsonb,
  state       text default 'active',                -- active | answered | expired
  answered_with jsonb,
  created_at  timestamptz default now()
);

create table if not exists attachments (
  id           uuid primary key default gen_random_uuid(),
  session_id   uuid references sessions(id) on delete cascade,
  storage_path text not null,
  mime         text,
  bytes        int,
  vlm_facts    jsonb default '{}'::jsonb,           -- caption, ocr_text, detected{}, safety_flags[]
  created_at   timestamptz default now()
);

create table if not exists tool_traces (
  id          uuid primary key default gen_random_uuid(),
  message_id  uuid references messages(id) on delete cascade,
  call_id     text,
  tool        text not null,
  args        jsonb,
  result      jsonb,
  ms          int,
  ok          boolean default true,
  created_at  timestamptz default now()
);

create table if not exists guard_hits (
  id          uuid primary key default gen_random_uuid(),
  message_id  uuid references messages(id) on delete cascade,
  rule_id     text not null,                        -- G1..G6
  detail      text,
  repaired    boolean default false,
  created_at  timestamptz default now()
);

create table if not exists tickets (
  id          uuid primary key default gen_random_uuid(),
  ticket_no   text unique,
  session_id  uuid references sessions(id) on delete set null,
  customer_id uuid references customers(id) on delete set null,
  product_id  uuid references products(id) on delete set null,
  priority    text default 'normal',                -- low | normal | high | urgent
  status      text default 'open',                  -- open | waiting_customer | escalated | resolved
  summary     text,
  verdict     text,                                 -- warranty verdict enum, when relevant
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

create table if not exists ticket_events (
  id         uuid primary key default gen_random_uuid(),
  ticket_id  uuid not null references tickets(id) on delete cascade,
  kind       text not null,
  payload    jsonb default '{}'::jsonb,
  created_at timestamptz default now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Evaluation
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists eval_cases (
  id        uuid primary key default gen_random_uuid(),
  name      text not null unique,
  scenario  text not null,                          -- S1 | S2 | S3 | S4 | edge
  input     jsonb not null,
  expect    jsonb not null,                         -- tools[], resolved_sku, verdict, no_guard_hits
  enabled   boolean default true
);

create table if not exists eval_runs (
  id         uuid primary key default gen_random_uuid(),
  case_id    uuid references eval_cases(id) on delete cascade,
  passed     boolean,
  score      jsonb,                                 -- judge rubric: empathy, clarity, proactivity, hallucination
  transcript jsonb,
  ms         int,
  cost       jsonb,
  created_at timestamptz default now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- RLS — the backend uses the secret key (bypasses RLS). The browser only ever
-- reads the catalog directly; everything else goes through the API.
-- ─────────────────────────────────────────────────────────────────────────────

alter table products       enable row level security;
alter table product_specs  enable row level security;
alter table product_media  enable row level security;
alter table error_codes    enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'products' and policyname = 'public read products') then
    create policy "public read products"      on products      for select using (true);
    create policy "public read product_specs" on product_specs for select using (true);
    create policy "public read product_media" on product_media for select using (true);
    create policy "public read error_codes"   on error_codes   for select using (true);
  end if;
end $$;

-- Everything else stays RLS-off and reachable only by the secret key.

-- ─────────────────────────────────────────────────────────────────────────────
-- Seed: warranty terms. The rule engine reads these; it never guesses.
-- ─────────────────────────────────────────────────────────────────────────────

insert into warranty_policies (category, months, covers, excludes) values
  ('robot_vacuum', 12, '["manufacturing defect","motor failure","sensor failure"]',
                       '["physical damage","liquid ingress","consumables: brush, filter"]'),
  ('breast_pump',  12, '["manufacturing defect","motor failure","battery defect"]',
                       '["physical damage","hygiene parts: flange, valve"]'),
  ('charger',      18, '["manufacturing defect","port failure"]', '["physical damage","cable wear"]'),
  ('power_station',24, '["manufacturing defect","cell degradation beyond spec"]', '["physical damage"]'),
  ('audio',        18, '["manufacturing defect","driver failure"]', '["physical damage","ear tips"]')
on conflict (category) do nothing;

-- Sanity check after seeding the catalog: scenario S2 needs the ambiguity to exist.
-- select alias, count(*) from product_aliases where alias = 's1 pro' group by alias;
--   → must return count >= 2
