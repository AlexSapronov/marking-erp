from fastapi import HTTPException
import pytest
from marking_erp.document_routes import normalize_lines
from marking_erp.invoice_excel import validate_rows


def test_merge_is_policy_controlled_and_counts_are_exact():
    line={'pn':'A','gtin':'04640638349704','tnved':'8542399000','quantity':10,'labels':3}
    assert normalize_lines([line,line])[0][0]['labels']==6
    assert len(normalize_lines([line,line],merge=False)[0])==2
    values=['A','04640638349704',10,3,'8542399000']
    assert len(validate_rows([values,values],merge=False)['lines'])==2


def test_incomplete_draft_is_not_a_verified_product():
    rows,_=normalize_lines([{'pn':'A','gtin':'','tnved':'','quantity':'1,0','labels':'2'}])
    assert rows[0]=={'pn':'A','gtin':'','tnved':'','quantity':1,'labels':2}
    with pytest.raises(HTTPException):normalize_lines([{**rows[0],'quantity':'1.5'}])
