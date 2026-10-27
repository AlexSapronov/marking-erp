"""Read-only catalog projection. Exact PN hiding never merges or archives cards."""

# Only published snapshots the caller can read participate. Local edits of a
# published card must not move the published PN to the edited, unconfirmed PN.
PUBLISHED = """(coalesce(body#>>'{external,raw,good_status}','')='published'
    OR coalesce(body#>'{external,statuses}','[]'::jsonb) ? 'published'
    OR coalesce(body#>'{external,raw,good_detailed_status}','[]'::jsonb) ? 'published')"""

CATALOG_CTE = """WITH visible AS MATERIALIZED (
    SELECT i.* FROM business_items i WHERE i.organization_id=:org AND i.kind='card'
    AND (:grant='all' OR (:grant='own' AND i.owner_id=:user)
      OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
), published AS MATERIALIZED (
    SELECT body FROM visible WHERE """ + PUBLISHED + """
), published_pns AS MATERIALIZED (
    SELECT DISTINCT identity.pn FROM published p CROSS JOIN LATERAL (
        SELECT min(btrim(a->>'attr_value')) AS pn FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.body#>'{external,raw,good_attrs}')='array'
              THEN p.body#>'{external,raw,good_attrs}' ELSE '[]'::jsonb END) a
        WHERE (CASE WHEN jsonb_path_exists(p.body,'$.external.raw.good_attrs[*] ? (@.attr_id == 13914 || @.attr_id == "13914")')
          THEN a->>'attr_id'='13914'
          ELSE regexp_replace(lower(coalesce(a->>'attr_name','')),'[^[:alnum:]_]','','g') IN
            ('модель','модельартикул','артикулпроизводителя','модельартикулпроизводителя','pn') END)
        AND btrim(coalesce(a->>'attr_value',''))<>''
        AND lower(btrim(a->>'attr_value'))<>lower(coalesce(p.body#>>'{external,raw,brand_name}',''))
        HAVING count(DISTINCT btrim(a->>'attr_value'))=1
    ) identity WHERE identity.pn IS NOT NULL
), catalog_rows AS (
    SELECT v.*, (v.state<>'archived' AND NOT """ + PUBLISHED + """
        AND (v.state='draft' OR coalesce(v.body#>'{external,statuses}','[]'::jsonb) ?| ARRAY['draft','errors']
          OR coalesce(v.body#>>'{external,raw,good_status}','')='draft')
        AND EXISTS(SELECT 1 FROM published_pns p WHERE p.pn=btrim(v.body->>'pn'))) AS duplicate_draft
    FROM visible v
) """

DUPLICATE_FILTER = """ AND (:archived OR :draft_duplicates='include'
    OR (:draft_duplicates='hide' AND NOT duplicate_draft)
    OR (:draft_duplicates='only' AND duplicate_draft)) """
