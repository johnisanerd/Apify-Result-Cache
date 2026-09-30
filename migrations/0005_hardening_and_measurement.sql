-- 0005: hardening and measurement for library 0.2.2. Apply after 0004, to the
-- cache's own project. Everything is idempotent.
--
--   cache_put            accepts only the key's own object path,
--                        <namespace>/<key[:2]>/<key>.json.gz. That is the one
--                        path the library has ever written to (DESIGN.md #18),
--                        so every existing row already satisfies it. The
--                        library's reader applies the same rule from 0.2.2.
--   cache_log            accepts the outcome 'failed': a miss whose fetch ended
--                        in a permanent source error (nothing to cache). Kept
--                        apart from 'miss' so the case for caching negative
--                        results can be measured before it is built.
--   cache_fragmentation  service-role report: paid requests whose entity was
--                        also requested under another key in the range (one
--                        video asked for with different language lists).
--
-- Grants: create or replace keeps a function's privileges, but they are
-- restated below so the file stands on its own.

-- --------------------------------------------------------------- cache_log

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
        or r.outcome not in ('logged', 'hit', 'miss', 'bypass', 'expired', 'error', 'failed')
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

-- --------------------------------------------------------------- cache_put
-- Same as 0003, except that blob_ref must be the key's own path.

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
    -- A row may only ever name the object written for its own key.
    if p_blob_ref is null
       or p_blob_ref <> p_namespace || '/' || left(p_key_hash, 2) || '/' || p_key_hash || '.json.gz' then
        raise exception 'blob_ref must be the key''s own path';
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

-- ----------------------------------------------------- cache_fragmentation
-- Paid requests in the range grouped by entity: how many entities were asked
-- for under more than one key, and how many requests those entities carried.
-- Service role only; exclusions are parameters, like cache_stats.

create or replace function public.cache_fragmentation(
    p_from               date,
    p_to                 date,
    p_namespace          text,
    p_exclude_user_hash  text   default null,
    p_exclude_entity_ids text[] default null
)
returns table (
    requests            bigint,
    entities            bigint,
    fragmented_entities bigint,
    fragmented_requests bigint
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
            select q.entity_id, q.key_hash
              from public.result_cache_requests q
             where q.namespace = p_namespace
               and q.is_paying
               and q.entity_id is not null
               and q.ts >= p_from::timestamptz
               and q.ts <  (p_to + 1)::timestamptz
               and (p_exclude_user_hash is null or q.user_hash <> p_exclude_user_hash)
               and (p_exclude_entity_ids is null or q.entity_id <> all(p_exclude_entity_ids))
        ), e as (
            select r.entity_id,
                   count(*)::bigint               as n,
                   count(distinct r.key_hash)::bigint as k
              from r
             group by r.entity_id
        )
        select coalesce(sum(e.n), 0)::bigint,
               count(*)::bigint,
               (count(*) filter (where e.k > 1))::bigint,
               coalesce(sum(e.n) filter (where e.k > 1), 0)::bigint
          from e;
end
$$;

-- ------------------------------------------------------------------ grants

revoke all on function public.cache_log(jsonb) from public, anon, authenticated;
revoke all on function public.cache_put(text, text, integer, text, text, text, integer, integer, text, boolean)
    from public, anon, authenticated;
revoke all on function public.cache_fragmentation(date, date, text, text, text[])
    from public, anon, authenticated;

grant execute on function public.cache_log(jsonb) to anon;
grant execute on function public.cache_put(text, text, integer, text, text, text, integer, integer, text, boolean)
    to anon;
grant execute on function public.cache_fragmentation(date, date, text, text, text[]) to service_role;
