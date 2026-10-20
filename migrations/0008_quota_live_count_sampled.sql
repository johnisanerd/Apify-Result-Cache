-- 0008 (2026-10-20). cache_quota's live-row count was still an exact count over
-- the whole index (1.5M rows, 920 MB): fine on warm pages, 8 s+ on a cold read
-- from the Micro instance's disk, which is how the first call of the day looks.
-- Count live rows from the same 2% page sample as the size mean. The quota
-- watch is a percentage with an 80% threshold; a sampled count is enough.

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
    select count(*) * 50, coalesce(avg(i.size_bytes), 0) into v_live, v_avg
      from public.result_cache_index i tablesample system (2)
     where i.expires_at > now();
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
