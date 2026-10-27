from marking_erp.business.catalog_sync import split_window,progress,SyncProblem
from marking_erp.business.errors import report_problem
import pytest


def test_report_rejection_guidance_does_not_echo_remote_values():
    result={'errors':[{'error':'Буфер GTIN 04640638341173 содержит ошибку: не найдена карточка товара или нет прав. secret=do-not-print'}]}
    code,message=report_problem(result)
    assert code=='catalog_not_ready' and 'позже' in message and 'ответственным' in message
    assert 'do-not-print' not in message and '04640638341173' not in message
    assert report_problem({'errors':['другая ошибка']})[0]=='report_rejected'


def test_windows_overlap_boundary_without_gaps_and_preserve_remaining_queue():
    body=progress({'from':'2026-01-01 00:00:00','to':'2026-01-01 00:00:05','offset':100,'count':100})
    assert body['count']==0 and body['windows'][0][2]==0
    split=split_window(body)
    assert split['windows']==[['2026-01-01 00:00:00','2026-01-01 00:00:02',0],['2026-01-01 00:00:02','2026-01-01 00:00:05',0]]
    with pytest.raises(SyncProblem):split_window({'windows':[['2026-01-01 00:00:00','2026-01-01 00:00:01',0]]})
