"""Follow confirmed replacements while keeping invoice/report/ATK snapshots intact."""
from fastapi import HTTPException
from sqlalchemy import text

def effective(c,ids):
    if not ids:return {}
    rows=c.execute(text('''WITH RECURSIVE chain(root,id,path) AS (
        SELECT id,id,ARRAY[id] FROM business_codes WHERE id=ANY(CAST(:ids AS uuid[]))
        UNION ALL SELECT x.root,r.new_code_id,x.path||r.new_code_id FROM chain x
        JOIN code_replacements r ON r.old_code_id=x.id WHERE NOT r.new_code_id=ANY(x.path) AND cardinality(x.path)<64)
        SELECT x.root,c.* FROM chain x JOIN business_codes c ON c.id=x.id
        WHERE NOT EXISTS(SELECT 1 FROM code_replacements r WHERE r.old_code_id=x.id)'''),{'ids':ids}).mappings().all()
    result={r['root']:dict(r) for r in rows}
    if len(result)!=len(set(ids)):raise HTTPException(409,'Не удалось однозначно установить актуальные КМ поставки')
    return result
