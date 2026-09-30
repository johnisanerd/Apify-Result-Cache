-- 0004: serving support for library 0.2. Apply after 0003, to the cache's own
-- project. Three service-role functions and the payload bucket:
--
--   cache_outcomes     request counts by (is_paying, outcome): the real hit
--                      rate once the cache serves, next to cache_stats' key
--                      repetition rate
--   cache_expired      expired index rows, oldest first, for the expiry job
--   cache_delete_index deletes index rows, but only ones already expired
--
-- Everything is idempotent. The anon key in Actor images can execute none of
-- these; serving itself needs only 0003's cache_lookup and cache_put.

create or replace function public.cache_outcomes(
    p_from               date,
    p_to                 date,
    p_namespace          text,
    p_exclude_user_hash  text   default null,
    p_exclude_entity_ids text[] default null
)
returns table (
    is_paying boolean,
    outcome   text,
    requests  bigint
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
        select q.is_paying, q.outcome, count(*)::bigint
          from public.result_cache_requests q
         where q.namespace = p_namespace
           and q.ts >= p_from::timestamptz
           and q.ts <  (p_to + 1)::timestamptz
           and (p_exclude_user_hash is null or q.user_hash <> p_exclude_user_hash)
           and (p_exclude_entity_ids is null or q.entity_id is null
                or q.entity_id <> all(p_exclude_entity_ids))
         group by q.is_paying, q.outcome
         order by q.is_paying desc, q.outcome;
end
$$;

create or replace function public.cache_expired(p_limit integer default 1000)
returns table (
    namespace  text,
    key_hash   text,
    blob_ref   text,
    expires_at timestamptz
)
language plpgsql
security definer
stable
set search_path = public
as $$
#variable_conflict use_column
begin
    if p_limit is null or p_limit < 1 or p_limit > 10000 then
        raise exception 'bad limit (1..10000)';
    end if;
    return query
        select i.namespace, i.key_hash, i.blob_ref, i.expires_at
          from public.result_cache_index i
         where i.expires_at < now()
         order by i.expires_at
         limit p_limit;
end
$$;

create or replace function public.cache_delete_index(p_namespace text, p_keys text[])
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count bigint;
begin
    if p_namespace is null or p_namespace = '' then
        raise exception 'namespace is required';
    end if;
    if p_keys is null or coalesce(array_length(p_keys, 1), 0) = 0 then
        return 0;
    end if;
    if array_length(p_keys, 1) > 10000 then
        raise exception 'too many keys (max 10000)';
    end if;

    -- Only rows that have already expired: this exists for the expiry job and
    -- must never remove a live entry, even if a key was re-stored meanwhile.
    delete from public.result_cache_index i
     where i.namespace = p_namespace
       and i.key_hash = any(p_keys)
       and i.expires_at < now();

    get diagnostics v_count = row_count;
    return v_count;
end
$$;

revoke all on function public.cache_outcomes(date, date, text, text, text[])
    from public, anon, authenticated;
revoke all on function public.cache_expired(integer)
    from public, anon, authenticated;
revoke all on function public.cache_delete_index(text, text[])
    from public, anon, authenticated;

grant execute on function public.cache_outcomes(date, date, text, text, text[]) to service_role;
grant execute on function public.cache_expired(integer)                         to service_role;
grant execute on function public.cache_delete_index(text, text[])               to service_role;

-- The payload bucket (Supabase Storage only; on another S3 backend create the
-- bucket there instead). Private: Actors reach it with S3 access keys, which
-- bypass storage policies, so no policies are defined. 2 MB matches
-- cache_put's size cap.
insert into storage.buckets (id, name, public, file_size_limit)
values ('result-cache', 'result-cache', false, 2000000)
on conflict (id) do nothing;
