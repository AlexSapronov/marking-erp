"""58×40 vector renderer adapted from audited marking-bm ce77a1cf; no emission/printing."""
from io import BytesIO
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode.ecc200datamatrix import ECC200DataMatrix
from .gs1 import parse

class GS1Matrix(ECC200DataMatrix):
    """ECC200 44x44 with ASCII encoding and FNC1 in first position.

    Keep arbitrary ASCII serial characters intact, including both bracket styles.
    ReportLab supplies Reed-Solomon and module placement; this only supplies the
    standard ASCII data codewords (ISO/IEC 16022), not a new barcode algorithm.
    """

    def _encode_c40(self, value):
        data, pos = [232], 0
        while pos < len(value):
            if (
                value[pos : pos + 2].isascii()
                and len(value[pos : pos + 2]) == 2
                and value[pos : pos + 2].isdigit()
            ):
                data.append(130 + int(value[pos : pos + 2]))
                pos += 2
            else:
                char = value[pos]
                if ord(char) > 127:
                    raise ValueError("КМ должен содержать только ASCII")
                # ASCII GS (0x1D) is an ordinary data byte, encoded as
                # codeword 30 in DataMatrix ASCII encodation. Only the
                # first symbol position is FNC1 (codeword 232).
                # Encoding separators as FNC1 (232) produces a visually
                # readable code but triggers the official CHZ validator.
                data.append(ord(char) + 1)
                pos += 1
        if len(data) > self.cw_data:
            raise ValueError("КМ слишком длинный для макета этикетки")
        if len(data) < self.cw_data:
            data.append(129)
        while len(data) < self.cw_data:
            value = 129 + ((149 * (len(data) + 1)) % 253) + 1
            data.append(value if value <= 254 else value - 254)
        return data


def render_label(raw:bytes,pn:str,quantity:int)->bytes:
    value=parse(raw)
    if type(quantity) is not int or quantity!=(value.quantity if value.quantity is not None else 1):raise ValueError('Количество не совпадает с содержимым КМ')
    if not isinstance(pn,str) or not pn.strip() or len(pn)>120 or any(ord(x)<32 for x in pn):raise ValueError('Некорректный PN')
    font_path=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    font='Helvetica'
    if font_path.is_file():
        if 'RegistryLabelSans' not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont('RegistryLabelSans',str(font_path)))
        font='RegistryLabelSans'
    elif not pn.isascii():raise ValueError('Для PN требуется шрифт DejaVu Sans на сервере')
    symbol=GS1Matrix(value.normalized.decode('ascii'));symbol.validate();matrix=symbol.encode()
    dots=int((24*mm/(72/203))//len(matrix));module=dots*72/203
    if dots<2:raise ValueError('Слишком плотный код для макета')
    size=len(matrix)*module;out=BytesIO();canvas=Canvas(out,pagesize=(58*mm,40*mm),pageCompression=1)
    x,y=2*mm,38*mm-size
    for row in range(len(matrix)):
        for col in range(len(matrix[row])):
            if matrix[row][col]:canvas.rect(x+col*module,y+row*module,module,module,fill=1,stroke=0)
    chunks=[];line=''
    for char in pn:
        if pdfmetrics.stringWidth(line+char,font,9)>26*mm:chunks.append(line);line=''
        line+=char
    chunks.append(line)
    if len(chunks)>4:raise ValueError('PN не помещается в макете')
    canvas.setFont(font,9)
    for i,line in enumerate(chunks):canvas.drawCentredString(42.5*mm,(32-i*4)*mm,line)
    canvas.setFont(font,11);canvas.drawCentredString(42.5*mm,9*mm,str(quantity)+(' шт.' if font!='Helvetica' else ' pcs'))
    canvas.setFont('Helvetica',5.4)
    for i,line in enumerate([value.cis[:19],value.cis[19:]]):canvas.drawCentredString(x+size/2,y-(3+i*2.8)*mm,line)
    canvas.showPage();canvas.save();return out.getvalue()
