from io import BytesIO
import zipfile
import pytest
from marking_erp import warehouse_excel as excel,invoice_excel
from test_warehouse_postgres import raw,GTIN


def workbook(rows):return invoice_excel.xlsx({'Товары':[excel.HEADERS,*rows]})


def test_full_codes_group_exact_pn_and_keep_unmarked_rows():
    data=workbook([['00123',GTIN,raw('a'),10,'8542399000'],['00123',GTIN,raw('b').replace('\x1d','<GS>'),10,'8542399000'],['00124',GTIN,raw('c'),10,'8542399000'],['NOMARK','','',7,'']])
    result=excel.read(data);assert not result['errors'];assert [r['pn'] for r in result['lines']]==['00123','00124','NOMARK']
    assert result['lines'][0]['labels']==2 and result['lines'][2]['marked'] is False
    assert result['expected_payloads'][raw('c')[:38]]['line_index']==1
    assert len(result['expected_codes'])==3 and result['unmarked_lines']==1


@pytest.mark.parametrize('pn,gtin,code,quantity,message',[
    (123,GTIN,raw(),10,'текстом'),('PN',GTIN,raw(),9,'Количество'),('PN',GTIN,raw()[:38],10,'криптохвост'),
    ('PN','04640638341173',raw(),10,'GTIN'),('PN','',raw(),10,'без GTIN'),('PN',GTIN,raw(),'1.5','целое'),
])
def test_row_errors_block_commit(pn,gtin,code,quantity,message):
    value=excel.read(workbook([[pn,gtin,code,quantity,'']]))
    assert value['error_count']==1 and message in value['errors'][0]['message']


def test_duplicate_cis_and_error_count_are_bounded():
    value=excel.read(workbook([['PN',GTIN,raw(),10,'']]*110))
    assert value['error_count']==109 and len(value['errors'])==100


def test_unit_without_ai30_and_safe_template():
    value=excel.read(workbook([['PN',GTIN,raw().split('\x1d30')[0],1,'']]))
    assert not value['errors'] and value['lines'][0]['quantity']==1
    assert excel.read(excel.template())['errors'][0]['message']=='Нет заполненных строк'


def test_formulas_and_unsafe_packages_rejected():
    content=workbook([['PN',GTIN,raw(),10,'']]);out=BytesIO()
    with zipfile.ZipFile(BytesIO(content)) as source,zipfile.ZipFile(out,'w') as target:
        for name in source.namelist():
            data=source.read(name)
            if name=='xl/worksheets/sheet1.xml':data=data.replace(b'<v>10</v>',b'<f>5+5</f><v>10</v>')
            target.writestr(name,data)
    with pytest.raises(ValueError,match='формулы'):excel.read(out.getvalue())


def test_printable_separator_notation_inside_serial_is_not_rewritten():
    serial='x<GS>'+'x'*15
    code='01'+GTIN+'21'+serial+'\x1d93<GS>\x1d3010'
    for value in (code,code.replace('\x1d','<GS>'),']d2'+code.replace('\x1d','_x001D_')):
        parsed=excel.parse_code(value)
        assert parsed.cis=='01'+GTIN+'21'+serial
        assert parsed.normalized==code.encode('ascii')
