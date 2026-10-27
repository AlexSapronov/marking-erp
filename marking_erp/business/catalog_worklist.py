"""Read-only catalog projection. Exact PN hiding never merges or archives cards."""

# Only published snapshots the caller can read participate. Local edits of a
# published card must not move the published PN to the edited, unconfirmed PN.
PUBLISHED = """(coalesce(body#>>'{external,raw,good_status}','')='published'
    OR coalesce(body#>'{external,statuses}','[]'::jsonb) ? 'published'
    OR coalesce(body#>'{external,raw,good_detailed_status}','[]'::jsonb) ? 'published')"""

CATALOG_CTE = """WITH visible AS NOT MATERIALIZED (
    SELECT i.* FROM business_items i WHERE i.organization_id=:org AND i.kind='card'
    AND (:grant='all' OR (:grant='own' AND i.owner_id=:user)
      OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
), published AS NOT MATERIALIZED (
    SELECT body FROM visible WHERE """ + PUBLISHED + """
), published_pns AS MATERIALIZED (
    SELECT DISTINCT identity.pn FROM published p CROSS JOIN LATERAL (
        SELECT min(value) AS pn FROM (
            SELECT btrim(coalesce(a->>'attr_value','')) AS value, a->>'attr_id'='13914' AS official,
                bool_or(a->>'attr_id'='13914') OVER () AS has_official
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.body#>'{external,raw,good_attrs}')='array'
                THEN p.body#>'{external,raw,good_attrs}' ELSE '[]'::jsonb END) a
            WHERE a->>'attr_id'='13914' OR regexp_replace(lower(coalesce(a->>'attr_name','')),'[^[:alnum:]_]','','g') IN
                ('модель','модельартикул','артикулпроизводителя','модельартикулпроизводителя','pn')
        ) candidates WHERE (official OR NOT coalesce(has_official,false)) AND value<>''
            AND lower(value)<>lower(coalesce(p.body#>>'{external,raw,brand_name}',''))
        HAVING count(DISTINCT value)=1
    ) identity WHERE identity.pn IS NOT NULL
), catalog_rows AS (
    SELECT v.*, (v.state<>'archived' AND NOT """ + PUBLISHED + """
        AND (v.state='draft' OR coalesce(v.body#>'{external,statuses}','[]'::jsonb) ?| ARRAY['draft','errors']
          OR coalesce(v.body#>>'{external,raw,good_status}','')='draft')
        AND coalesce(btrim(v.body->>'pn'),'') IN (SELECT pn FROM published_pns)) AS duplicate_draft
    FROM visible v
) """

DUPLICATE_FILTER = """ AND (:archived OR :draft_duplicates='include'
    OR (:draft_duplicates='hide' AND NOT duplicate_draft)
    OR (:draft_duplicates='only' AND duplicate_draft)) """
