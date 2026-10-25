from io import BytesIO
from pathlib import Path
import pytest
import fitz
from PIL import Image
from pypdf import PdfReader
import zxingcpp
from marking_erp.gs1 import GS,RS,parse,with_quantity,division_entry,valid_gtin
from marking_erp.labels import GS1Matrix,render_label

GTIN='04640638349704'
SERIAL=b'5pZpV1RbL3/uSA&!*"FB'

def sample(template=36,serial=SERIAL,quantity=None):
    raw=b'01'+GTIN.encode()+b'21'+serial+(GS+b'91EE12'+GS+b'92'+(b'30aB/+=91_92'*4)[:44] if template==36 else GS+b'933091')
    return with_quantity(raw,quantity) if quantity is not None else raw


@pytest.mark.parametrize('template',[36,37])
@pytest.mark.parametrize('quantity',[None,1,50,99999999])
def test_strict_byte_corpus_and_scanner_transport(template,quantity):
    raw=sample(template,quantity=quantity);value=parse(raw)
    assert value.raw==value.normalized==raw and value.template==template and value.quantity==quantity
    assert valid_gtin(value.gtin) and value.cis==raw[:38].decode()
    scanner=parse(b']d2'+raw+b'\r\n',scanner=True)
    assert scanner.normalized==raw and scanner.raw==b']d2'+raw+b'\r\n' and len(scanner.corrections)==2
    with pytest.raises(ValueError):parse(scanner.raw)


@pytest.mark.parametrize('serial',[b'5309192_[]{}<>@/#?ab',b'5literal{GS}^]309192',SERIAL])
def test_literal_tokens_and_ai_strings_inside_serial_are_preserved(serial):
    assert len(serial)==20
    raw=sample(36,serial,12);assert parse(raw).normalized==raw


@pytest.mark.parametrize('bad',[b'',b']d2'+sample(),sample().replace(GS,b''),sample()+b'\r\n',sample()+GS+b'300',sample()+GS+b'30100000000',sample()+b'3050',sample()+GS+b'31030005',sample().replace(b'EE12',b'EE\x1e2'),sample()[:-1],sample().replace(GTIN.encode(),b'04640638349705'),sample().replace(SERIAL,'Русская раскладка'.encode())])
def test_rejects_lossy_ambiguous_unknown_or_invalid_inputs(bad):
    with pytest.raises(ValueError):parse(bad)


@pytest.mark.parametrize('template',[36,37])
def test_s004_source_report_omits_ai30_without_mutating_original(template):
    source=sample(template,quantity=100);new=sample(template,SERIAL[:-1]+b'X',25)
    pair=division_entry(new,source)
    assert pair==new+RS+parse(source).base and source.endswith(GS+b'30100')
    assert pair.split(RS)[1]==parse(source).base
    with pytest.raises(ValueError):division_entry(source,source)


@pytest.mark.parametrize('template',[36,37])
@pytest.mark.parametrize('dpi',[203,300])
def test_actual_pdf_decode_dimensions_and_codewords(template,dpi):
    raw=sample(template,quantity=12345);data=render_label(raw,'L-132XYD',12345)
    pdf=PdfReader(BytesIO(data));page=pdf.pages[0]
    assert abs(float(page.mediabox.width)-58*72/25.4)<.01 and abs(float(page.mediabox.height)-40*72/25.4)<.01
    with fitz.open(stream=data,filetype='pdf') as document:
        pix=document[0].get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72),alpha=False)
        img=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
        decoded=zxingcpp.read_barcodes(img)
        assert len(decoded)==1 and decoded[0].bytes==raw and decoded[0].symbology_identifier==']d2'
    words=GS1Matrix(raw.decode())._encode_c40(raw.decode());words=words[:words.index(129)]
    assert words[0]==232 and words.count(232)==1 and words.count(30)==raw.count(GS)


def test_pdf_does_not_lie_about_quantity_or_truncate_pn():
    with pytest.raises(ValueError):render_label(sample(37,quantity=50),'PN',49)
    with pytest.raises(ValueError):render_label(sample(37),'X'*120,1)
    assert render_label(sample(37),'PN [A] / <B>',1).startswith(b'%PDF')
