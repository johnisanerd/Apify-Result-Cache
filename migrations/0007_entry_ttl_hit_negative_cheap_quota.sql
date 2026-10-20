-- 0007 (2026-10-20). Three things:
--
--   cache_log      accepts the outcome 'hit_negative': a remembered "no result"
--                  verdict served from the cache (library 0.5.1, YouTube
--                  Actor). Until now a served verdict logged plain 'hit', so
--                  the weekly read could not tell transcripts from verdicts
--                  and fell back to guessing from object size.
--   cache_quota    stops scanning the heap. At 1.5M index rows and 2M log rows
--                  the 0003 version (count(*), sum(size_bytes), and a 30-day
--                  hits-to-index join) hit the API's 8 s statement timeout
--                  (57014). cache_gc.py gates its real run on this call, so the
--                  GC task has been inert since the tables grew. Counts that an
--                  index makes cheap stay exact; the rest are estimates from a
--                  2% page sample and pg_class.reltuples, which is all an 80%
--                  quota watch needs.
--   per-entry TTL  needs no schema change: cache_put has taken p_ttl_days per
--                  row since 0003. Library 0.5.1 passes a shorter value for
--                  entries that must not outlive their namespace default.

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

    -- Breaker. With the spend cap on, the disk cannot grow past the plan's
    -- included 8 GB, and a full disk puts the whole project in read-only
    -- mode. Stopping the log at 4 GB leaves room for the index.
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
        or r.outcome not in ('logged', 'hit', 'hit_negative', 'miss', 'bypass', 'expired', 'error', 'failed')
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
declare
    v_live   bigint;
    v_avg    numeric;
    v_hits30 bigint;
begin
    select count(*) into v_live
      from public.result_cache_index i where i.expires_at > now();
    -- Mean object size from a 2% page sample; times the live count.
    select coalesce(avg(i.size_bytes), 0) into v_avg
      from public.result_cache_index i tablesample system (2);
    -- Served requests in 30 days from the same 2% sample, scaled back up.
    select count(*) into v_hits30
      from public.result_cache_requests r tablesample system (2)
     where r.outcome in ('hit', 'hit_negative') and r.ts >= now() - interval '30 days';
    return query
        select pg_database_size(current_database())::bigint,
               pg_total_relation_size('public.result_cache_requests')::bigint,
               pg_total_relation_size('public.result_cache_index')::bigint,
               greatest((select c.reltuples from pg_class c
                          where c.oid = 'public.result_cache_requests'::regclass), 0)::bigint,
               v_live,
               (v_live * v_avg)::bigint,
               (v_hits30 * 50 * v_avg)::bigint,
               (select count(*) from public.result_cache_requests r
                 where r.ts >= now() - interval '24 hours')::bigint,
               (select count(*) from public.result_cache_requests r
                 where r.ts >= now() - interval '24 hours' and r.outcome = 'error')::bigint;
end
$$;
