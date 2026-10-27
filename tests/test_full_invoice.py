from io import BytesIO
import pytest
from openpyxl import Workbook
from marking_erp.business.full_invoice import parse

GTIN='04640638349704'
EXPECTED=[{'pn':'00123','gtin':GTIN,'quantity':10,'labels':2}]


def excel(rows):
    book=Workbook();sheet=book.active
    for row in rows:sheet.append(row)
    data=BytesIO();book.save(data);return data.getvalue()


def test_full_quantities_aggregate_and_information_keeps_duplicate_rows():
    data=excel([['PN','GTIN','Количество'],['00123',GTIN,15],['00123',GTIN,5],['BOLT',None,2],['BOLT',None,3]])
    result=parse(data,EXPECTED)
    assert not result['errors'] and [r['quantity'] for r in result['lines']]==[2,3]
    assert all(r['marked'] is False and r['labels']==0 for r in result['lines'])


@pytest.mark.parametrize('pn,gtin,qty',[('00123',None,20),('00123',GTIN,21),('OTHER',GTIN,20),('00123',GTIN,'=10+10')])
def test_cannot_change_or_hide_already_marked_positions(pn,gtin,qty):
    result=parse(excel([['PN','GTIN','Количество'],[pn,gtin,qty]]),EXPECTED)
    assert result['errors']


def test_unrecognized_headers_require_mapping_and_explicit_mapping_works():
    data=excel([['Артикул из 1С','Наш код','Остаток'],['00123',GTIN,20],['BOLT','',5]])
    assert parse(data,EXPECTED)['requires_mapping']
    result=parse(data,EXPECTED,sheet_name='Sheet',header_row=1,mapping={'pn':0,'gtin':1,'quantity':2})
    assert not result['errors'] and result['lines'][0]['pn']=='BOLT'
