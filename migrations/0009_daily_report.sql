-- 0009 (2026-11-20). One read-only call for the daily report that cache_gc.py
-- prints after its run: where the database space goes and what each namespace
-- did in the last 24 hours. cache_quota() stays as it is.
--
-- The API role keeps an 8 s statement timeout and a pass over the index (2.5M
-- rows, 1.3 GB) is bound by the Micro instance's disk: 7 s warm, and a 20%
-- page sample still took 12 s cold. So the per-namespace index figures come
-- from a 5% page sample, times twenty. They are estimates: good for the large
-- namespaces the report draws conclusions from, noisy for the small ones.
-- The expired backlog (through the expires_at index) and the 24-hour request
-- figures (about 60k rows through the ts index) are exact.

create or replace function public.cache_daily_report()
returns jsonb
language plpgsql
security definer
stable
set search_path = public
as $$
declare
    v_index    jsonb;
    v_requests jsonb;
    v_top      jsonb;
begin
    select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) into v_index
      from (
        select i.namespace,
               count(*) * 20                                                        as rows,
               coalesce(sum(i.size_bytes), 0)::bigint * 20                          as payload_bytes,
               count(*) filter (where i.expires_at < now()) * 20                    as expired_waiting,
               count(*) filter (where i.expires_at < now() + interval '7 days') * 20 as expiring_7d,
               count(*) filter (where i.fetched_at > now() - interval '1 day') * 20 as stored_24h,
               count(*) filter (where i.hit_count = 0) * 20                         as never_hit,
               count(*) filter (where i.fetched_at < now() - interval '7 days') * 20 as over_7d,
               count(*) filter (where i.fetched_at < now() - interval '7 days'
                                  and i.last_hit_at > i.fetched_at + interval '7 days') * 20 as useful_past_7d,
               count(*) filter (where i.fetched_at < now() - interval '30 days') * 20 as over_30d,
               count(*) filter (where i.fetched_at < now() - interval '30 days'
                                  and i.last_hit_at > i.fetched_at + interval '30 days') * 20 as useful_past_30d,
               round(extract(epoch from max(i.expires_at - i.fetched_at)) / 86400.0, 1) as ttl_days
          from public.result_cache_index i tablesample system (5)
         group by i.namespace
      ) t;

    select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) into v_requests
      from (
        select r.namespace, r.outcome, r.is_paying,
               count(*) as requests, count(distinct r.user_hash) as users
          from public.result_cache_requests r
         where r.ts >= now() - interval '24 hours'
         group by r.namespace, r.outcome, r.is_paying
      ) t;

    -- The busiest single caller per namespace (a count only, no identity).
    select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) into v_top
      from (
        select u.namespace, max(u.n) as top_user_requests
          from (select r.namespace, r.user_hash, count(*) as n
                  from public.result_cache_requests r
                 where r.ts >= now() - interval '24 hours'
                 group by r.namespace, r.user_hash) u
         group by u.namespace
      ) t;

    return jsonb_build_object(
        'generated_at', now(),
        'db_bytes', pg_database_size(current_database()),
        'storage_objects_bytes', pg_total_relation_size('storage.objects'),
        'storage_objects_rows', greatest((select c.reltuples from pg_class c
                                           where c.oid = 'storage.objects'::regclass), 0)::bigint,
        'requests_bytes', pg_total_relation_size('public.result_cache_requests'),
        'request_rows', greatest((select c.reltuples from pg_class c
                                   where c.oid = 'public.result_cache_requests'::regclass), 0)::bigint,
        'index_bytes', pg_total_relation_size('public.result_cache_index'),
        'index_rows', greatest((select c.reltuples from pg_class c
                                 where c.oid = 'public.result_cache_index'::regclass), 0)::bigint,
        'expired_waiting', (select count(*) from public.result_cache_index i where i.expires_at < now()),
        'oldest_request_ts', (select min(r.ts) from public.result_cache_requests r),
        'index', v_index,
        'requests_24h', v_requests,
        'top_users_24h', v_top
    );
end
$$;

revoke all on function public.cache_daily_report()
    from public, anon, authenticated;
grant execute on function public.cache_daily_report() to service_role;
