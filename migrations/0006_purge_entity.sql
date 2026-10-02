-- 0006: purge every cached result for one entity (an opt-out or removal request).
--
-- cache_purge_entity deletes the LIVE index rows whose entity_id matches, in
-- one namespace or in a list of them, and returns their blob refs so the
-- operator script can delete the objects from the bucket. It also blanks the
-- entity_id in the request log so the request history no longer names the
-- person. Service role only; the anon key in Actor images cannot call it.
--
-- Idempotent.

create or replace function public.cache_purge_entity(
    p_namespaces text[],
    p_entity_id  text
)
returns table (
    namespace text,
    key_hash  text,
    blob_ref  text
)
language plpgsql
security definer
set search_path = public
as $$
#variable_conflict use_column
begin
    if p_entity_id is null or btrim(p_entity_id) = '' then
        raise exception 'entity_id is required';
    end if;
    if p_namespaces is null or coalesce(array_length(p_namespaces, 1), 0) = 0 then
        raise exception 'at least one namespace is required';
    end if;
    if array_length(p_namespaces, 1) > 50 then
        raise exception 'too many namespaces (max 50)';
    end if;

    update public.result_cache_requests r
       set entity_id = null
     where r.namespace = any(p_namespaces)
       and r.entity_id = p_entity_id;

    return query
    delete from public.result_cache_index i
     where i.namespace = any(p_namespaces)
       and i.entity_id = p_entity_id
    returning i.namespace, i.key_hash, i.blob_ref;
end
$$;

revoke all on function public.cache_purge_entity(text[], text)
    from public, anon, authenticated;
grant execute on function public.cache_purge_entity(text[], text) to service_role;
