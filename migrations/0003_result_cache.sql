-- Result cache for the Actor fleet: an append-only request log (Phase 0
-- onward) and an index of cached payloads (Phase 1). Payload bytes never live
-- in Postgres; they go to an S3-compatible bucket and the index holds the
-- reference, the digest and the expiry.
--
-- Security model, same as 0001: the anon key rides in every Actor's
-- environment and must be assumed public. RLS is on with ZERO policies and
-- direct grants are revoked, so the key cannot touch the tables at all. The
-- only reachable surface is the SECURITY DEFINER functions below, each of
-- which validates and caps its input. The three read/aggregate functions
-- (cache_stats, cache_quota, cache_gc_requests) are granted to service_role
-- only: fleet-wide numbers and deletes are not for the key that ships in
-- Actor images.
--
-- Everything here is idempotent so the file can be re-applied.

-- ------------------------------------------------------------------ tables

create table if not exists public.result_cache_requests (
    id           bigint generated always as identity primary key,
    ts           timestamptz not null default now(),   -- server clock, never the Actor's
    namespace    text        not null,                 -- e.g. 'youtube-transcript'
    key_hash     text        not null,                 -- sha256 hex of the canonical key
    entity_id    text,                                 -- e.g. video_id; not PII; enables "what is popular"
    actor_id     text        not null,
    user_hash    text        not null,                 -- sha256(APIFY_USER_ID): same-user vs cross-user repeats
    is_paying    boolean     not null,
    outcome      text        not null                  -- logged | hit | miss | bypass | expired | error
);
create index if not exists result_cache_requests_ns_key_ts_idx
    on public.result_cache_requests (namespace, key_hash, ts);
create index if not exists result_cache_requests_ts_idx
    on public.result_cache_requests (ts);

create table if not exists public.result_cache_index (
    namespace        text        not null,
    key_hash         text        not null,
    schema_version   integer     not null,
    entity_id        text,
    blob_ref         text        not null,             -- '<namespace>/<yyyy>/<mm>/<key_hash>.json.gz'
    sha256           text        not null,             -- of the gzipped blob; verified on read
    size_bytes       integer     not null,
    fetched_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    writer_actor_id  text        not null,
    writer_is_paying boolean     not null,
    hit_count        integer     not null default 0,
    last_hit_at      timestamptz,
    primary key (namespace, key_hash)
);
create index if not exists result_cache_index_expires_idx
    on public.result_cache_index (expires_at);

alter table public.result_cache_requests enable row level security;
alter table public.result_cache_index    enable row level security;
-- Deliberately no policies. RLS with no policy = deny all for anon.
revoke all on table public.result_cache_requests from anon, authenticated;
revoke all on table public.result_cache_index    from anon, authenticated;

-- --------------------------------------------------------------- cache_log
-- Bulk insert into the request log. Whole batch or nothing: one bad row
-- rejects the call, and the client drops the batch rather than retrying it
-- (a retry after a timeout could double-count keys).

create or replace function public.cache_log(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_n     integer;
    v_bad   integer;
    v_count integer;
begin
    if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
        raise exception 'p_rows must be a JSON array';
    end if;

    v_n := jsonb_array_length(p_rows);
    if v_n = 0 then
        return 0;
    end if;
    if v_n > 500 then
        raise exception 'too many rows (max 500)';
    end if;

    -- Breaker. The project is shared with the free-tier ledger; a runaway log
    -- must fail on itself, never fill the disk under the limiter. 4 GB is
    -- half of the Pro plan's included disk.
    if pg_total_relation_size('public.result_cache_requests') > 4::bigint * 1024 * 1024 * 1024 then
        raise exception 'request log is full';
    end if;

    -- Strict types before the record cast: Postgres would otherwise accept the
    -- strings 'yes', 'on', 't' or '1' as a boolean.
    if exists (select 1
                 from jsonb_array_elements(p_rows) as e(v)
                where jsonb_typeof(e.v) <> 'object'
                   or jsonb_typeof(e.v -> 'is_paying') is distinct from 'boolean') then
        raise exception 'bad row shape';
    end if;

    select count(*) into v_bad
      from jsonb_to_recordset(p_rows)
           as r(namespace text, key_hash text, entity_id text, actor_id text,
                user_hash text, is_paying boolean, outcome text)
     where r.namespace is null or r.namespace = '' or length(r.namespace) > 64
        or r.key_hash  is null or r.key_hash  !~ '^[0-9a-f]{64}$'
        or r.actor_id  is null or r.actor_id  = '' or length(r.actor_id) > 64
        or r.user_hash is null or r.user_hash !~ '^[0-9a-f]{64}$'
        or r.is_paying is null
        or r.outcome   is null
        or r.outcome not in ('logged', 'hit', 'miss', 'bypass', 'expired', 'error')
        or length(coalesce(r.entity_id, '')) > 128;
    if v_bad > 0 then
        raise exception 'bad row shape';
    end if;

    insert into public.result_cache_requests
        (namespace, key_hash, entity_id, actor_id, user_hash, is_paying, outcome)
    select r.namespace, r.key_hash, r.entity_id, r.actor_id, r.user_hash, r.is_paying, r.outcome
      from jsonb_to_recordset(p_rows)
           as r(namespace text, key_hash text, entity_id text, actor_id text,
                user_hash text, is_paying boolean, outcome text);

    get diagnostics v_count = row_count;
    return v_count;
end
$$;

-- ------------------------------------------------------------ cache_lookup
-- Index rows for up to 100 keys that are inside the caller's freshness window
-- and not expired. Bumps hit_count/last_hit_at for what it returns, in the
-- same statement, so the counter and the answer cannot disagree.

create or replace function public.cache_lookup(
    p_namespace    text,
    p_keys         text[],
    p_max_age_days integer
)
returns table (
    key_hash       text,
    blob_ref       text,
    sha256         text,
    size_bytes     integer,
    fetched_at     timestamptz,
    schema_version integer
)
language plpgsql
security definer
set search_path = public
as $$
#variable_conflict use_column
declare
    v_days integer;
begin
    if p_namespace is null or p_namespace = '' then
        raise exception 'namespace is required';
    end if;
    if p_keys is null or coalesce(array_length(p_keys, 1), 0) = 0
       or p_max_age_days is null or p_max_age_days <= 0 then
        return;                                      -- 0 = "always fresh": nothing to serve
    end if;
    if array_length(p_keys, 1) > 100 then
        raise exception 'too many keys (max 100)';
    end if;
    v_days := least(p_max_age_days, 365);

    return query
        with hit as (
            update public.result_cache_index as i
               set hit_count   = i.hit_count + 1,
                   last_hit_at = now()
             where i.namespace  = p_namespace
               and i.key_hash   = any(p_keys)
               and i.expires_at > now()
               and i.fetched_at >= now() - make_interval(days => v_days)
            returning i.key_hash, i.blob_ref, i.sha256, i.size_bytes, i.fetched_at, i.schema_version
        )
        select hit.key_hash, hit.blob_ref, hit.sha256, hit.size_bytes, hit.fetched_at, hit.schema_version
          from hit;
end
$$;

-- --------------------------------------------------------------- cache_put
-- Upsert one index row. The counters describe the current payload, so a
-- rewrite resets them.

create or replace function public.cache_put(
    p_namespace      text,
    p_key_hash       text,
    p_schema_version integer,
    p_entity_id      text,
    p_blob_ref       text,
    p_sha256         text,
    p_size_bytes     integer,
    p_ttl_days       integer,
    p_actor_id       text,
    p_is_paying      boolean
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_namespace is null or p_namespace = '' or length(p_namespace) > 64 then
        raise exception 'namespace is required';
    end if;
    if p_key_hash is null or p_key_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'bad key_hash';
    end if;
    if p_sha256 is null or p_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'bad sha256';
    end if;
    if p_schema_version is null or p_schema_version < 1 then
        raise exception 'bad schema_version';
    end if;
    if p_size_bytes is null or p_size_bytes < 1 or p_size_bytes > 2000000 then
        raise exception 'size_bytes out of range (1..2000000)';
    end if;
    if p_ttl_days is null or p_ttl_days < 1 or p_ttl_days > 365 then
        raise exception 'ttl_days out of range (1..365)';
    end if;
    if p_actor_id is null or p_actor_id = '' or length(p_actor_id) > 64 then
        raise exception 'actor_id is required';
    end if;
    if p_is_paying is null then
        raise exception 'is_paying is required';
    end if;
    if length(coalesce(p_entity_id, '')) > 128 then
        raise exception 'entity_id too long';
    end if;
    -- A key holder must not point one namespace's row at another's blob.
    if p_blob_ref is null or p_blob_ref = '' or length(p_blob_ref) > 256
       or left(p_blob_ref, length(p_namespace) + 1) <> p_namespace || '/' then
        raise exception 'blob_ref must be inside the namespace';
    end if;

    insert into public.result_cache_index as i
        (namespace, key_hash, schema_version, entity_id, blob_ref, sha256, size_bytes,
         fetched_at, expires_at, writer_actor_id, writer_is_paying, hit_count, last_hit_at)
    values
        (p_namespace, p_key_hash, p_schema_version, p_entity_id, p_blob_ref, p_sha256, p_size_bytes,
         now(), now() + make_interval(days => p_ttl_days), p_actor_id, p_is_paying, 0, null)
    on conflict (namespace, key_hash) do update
        set schema_version   = excluded.schema_version,
            entity_id        = excluded.entity_id,
            blob_ref         = excluded.blob_ref,
            sha256           = excluded.sha256,
            size_bytes       = excluded.size_bytes,
            fetched_at       = excluded.fetched_at,
            expires_at       = excluded.expires_at,
            writer_actor_id  = excluded.writer_actor_id,
            writer_is_paying = excluded.writer_is_paying,
            hit_count        = 0,
            last_hit_at      = null;
end
$$;

-- ------------------------------------------------------------- cache_stats
-- The hit-rate gate, one row of aggregates for a namespace and a date range
-- (inclusive, whole days). Paying callers only. Nothing namespace-specific
-- lives here: exclusions (example IDs, the owner's own probe runs) are
-- parameters supplied by scripts/cache_stats.py.

create or replace function public.cache_stats(
    p_from               date,
    p_to                 date,
    p_namespace          text,
    p_exclude_user_hash  text   default null,
    p_exclude_entity_ids text[] default null
)
returns table (
    requests                bigint,
    distinct_keys           bigint,
    hit_rate_any_user       double precision,
    hit_rate_same_user_only double precision,
    distinct_users          bigint,
    first_ts                timestamptz,
    last_ts                 timestamptz
)
language plpgsql
security definer
stable
set search_path = public
as $$
#variable_conflict use_column
begin
    if p_from is null or p_to is null or p_to < p_from then
        raise exception 'bad date range';
    end if;
    if p_to - p_from > 366 then
        raise exception 'date range too long (max 366 days)';
    end if;
    if p_namespace is null or p_namespace = '' then
        raise exception 'namespace is required';
    end if;

    return query
        with r as (
            select q.key_hash, q.user_hash, q.ts
              from public.result_cache_requests q
             where q.namespace = p_namespace
               and q.is_paying
               and q.ts >= p_from::timestamptz
               and q.ts <  (p_to + 1)::timestamptz
               and (p_exclude_user_hash is null or q.user_hash <> p_exclude_user_hash)
               and (p_exclude_entity_ids is null or q.entity_id is null
                    or q.entity_id <> all(p_exclude_entity_ids))
        )
        select count(*)::bigint,
               count(distinct r.key_hash)::bigint,
               1 - count(distinct r.key_hash)::double precision
                     / nullif(count(*), 0)::double precision,
               1 - count(distinct (r.key_hash, r.user_hash))::double precision
                     / nullif(count(*), 0)::double precision,
               count(distinct r.user_hash)::bigint,
               min(r.ts),
               max(r.ts)
          from r;
end
$$;

-- ------------------------------------------------------------- cache_quota
-- What the weekly quota watch reads: sizes against the plan's included
-- quotas, plus the last day's error share. Service role only.

create or replace function public.cache_quota()
returns table (
    db_bytes         bigint,
    requests_bytes   bigint,
    index_bytes      bigint,
    request_rows     bigint,
    index_rows_live  bigint,
    live_blob_bytes  bigint,
    egress_30d_bytes bigint,
    rows_24h         bigint,
    error_rows_24h   bigint
)
language plpgsql
security definer
stable
set search_path = public
as $$
#variable_conflict use_column
begin
    return query
        select pg_database_size(current_database())::bigint,
               pg_total_relation_size('public.result_cache_requests')::bigint,
               pg_total_relation_size('public.result_cache_index')::bigint,
               (select count(*) from public.result_cache_requests)::bigint,
               (select count(*) from public.result_cache_index i
                 where i.expires_at > now())::bigint,
               (select coalesce(sum(i.size_bytes), 0) from public.result_cache_index i
                 where i.expires_at > now())::bigint,
               (select coalesce(sum(i.size_bytes), 0)
                  from public.result_cache_requests r
                  join public.result_cache_index i
                    on i.namespace = r.namespace and i.key_hash = r.key_hash
                 where r.outcome = 'hit'
                   and r.ts >= now() - interval '30 days')::bigint,
               (select count(*) from public.result_cache_requests r
                 where r.ts >= now() - interval '24 hours')::bigint,
               (select count(*) from public.result_cache_requests r
                 where r.ts >= now() - interval '24 hours'
                   and r.outcome = 'error')::bigint;
end
$$;

-- ------------------------------------------------------- cache_gc_requests
-- Retention for the request log: delete rows older than p_before, in bounded
-- batches so no single call holds a long lock. Refuses anything younger than
-- 7 days, so a typo cannot erase the measurement window. Service role only.

create or replace function public.cache_gc_requests(
    p_before    timestamptz,
    p_namespace text    default null,
    p_limit     integer default 50000
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count bigint;
begin
    if p_before is null or p_before > now() - interval '7 days' then
        raise exception 'p_before must be at least 7 days in the past';
    end if;
    if p_limit is null or p_limit < 1 or p_limit > 100000 then
        raise exception 'bad limit (1..100000)';
    end if;

    delete from public.result_cache_requests q
     where q.id in (
           select o.id
             from public.result_cache_requests o
            where o.ts < p_before
              and (p_namespace is null or o.namespace = p_namespace)
            order by o.id
            limit p_limit);

    get diagnostics v_count = row_count;
    return v_count;
end
$$;

-- ------------------------------------------------------------------ grants
-- Supabase's default privileges grant EXECUTE on new public functions to
-- anon, authenticated and service_role, and `revoke ... from public` does not
-- remove those role-specific grants (the 0002 lesson). Revoke per role, then
-- grant exactly what each caller needs.

revoke all on function public.cache_log(jsonb)
    from public, anon, authenticated;
revoke all on function public.cache_lookup(text, text[], integer)
    from public, anon, authenticated;
revoke all on function public.cache_put(text, text, integer, text, text, text, integer, integer, text, boolean)
    from public, anon, authenticated;
revoke all on function public.cache_stats(date, date, text, text, text[])
    from public, anon, authenticated;
revoke all on function public.cache_quota()
    from public, anon, authenticated;
revoke all on function public.cache_gc_requests(timestamptz, text, integer)
    from public, anon, authenticated;

grant execute on function public.cache_log(jsonb)                         to anon;
grant execute on function public.cache_lookup(text, text[], integer)      to anon;
grant execute on function public.cache_put(text, text, integer, text, text, text, integer, integer, text, boolean)
    to anon;
grant execute on function public.cache_stats(date, date, text, text, text[])   to service_role;
grant execute on function public.cache_quota()                                 to service_role;
grant execute on function public.cache_gc_requests(timestamptz, text, integer) to service_role;
