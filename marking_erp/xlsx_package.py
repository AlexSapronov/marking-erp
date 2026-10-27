"""Bounded, passive OOXML package shared by business importers."""
from io import BytesIO
import zipfile
from xml.dom import minidom
from urllib.parse import urlsplit
MAIN='http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def xml(data):
    # Accept UTF-8 OOXML only. Reject DTD/entity declarations before the DOM parser.
    value=data.decode('utf-8-sig')
    if '\x00' in value or '<!DOCTYPE' in value.upper() or '<!ENTITY' in value.upper():
        raise ValueError('Недопустимые объявления XML в Excel')
    return minidom.parseString(value.encode('utf-8'))


def package(content):
    if len(content)>10*1024*1024:raise ValueError('Файл больше 10 МБ')
    archive=zipfile.ZipFile(BytesIO(content));infos=archive.infolist();names=[i.filename for i in infos]
    if len(names)>1500 or len(set(names))!=len(names) or sum(i.file_size for i in infos)>40*1024*1024:
        archive.close();raise ValueError('Слишком большой или неоднозначный XLSX')
    try:
        for info in infos:
            name=info.filename
            if name.startswith('/') or '\\' in name or '..' in name.split('/') or info.flag_bits&1:
                raise ValueError('Недопустимая структура XLSX')
            lowered=name.lower()
            if any(part in lowered for part in ('vbaproject','activex/','embeddings/','externallinks/','_xmlsignatures/')):
                raise ValueError('Для импорта нужна книга XLSX без макросов, вложений и внешних подключений')
            if lowered.endswith(('.xml','.rels')):
                if info.file_size>8*1024*1024:raise ValueError('Слишком большой XML-лист Excel')
                doc=xml(archive.read(info))
                try:
                    if doc.getElementsByTagNameNS(MAIN,'f'):
                        raise ValueError('Сохраните Excel как значения: формулы в партии импорта не поддерживаются')
                    for rel in doc.getElementsByTagNameNS('*','Relationship'):
                        if rel.getAttribute('TargetMode').lower()=='external' and not (rel.getAttribute('Type').endswith('/hyperlink') and urlsplit(rel.getAttribute('Target')).scheme.lower() in ('https','http','mailto')):
                            raise ValueError('Удалите внешние ссылки из книги Excel перед импортом')
                finally:doc.unlink()
        return archive
    except Exception:
        archive.close();raise


