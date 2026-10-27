"""Durable date windows for the verified NK /v4/product-list contract."""
from datetime import datetime

FORMAT='%Y-%m-%d %H:%M:%S'


class SyncProblem(ValueError):pass


def progress(body):
    if 'windows' in body:return {**body,'windows':[list(w) for w in body['windows']]}
    # Older jobs have no per-good receipts; restart their read-only traversal so
    # duplicates and counts are handled consistently after upgrading.
    return {**body,'windows':[[body['from'],body['to'],0]],'count':0}


def split_window(body):
    start,end,_=body['windows'][0]
    a,b=datetime.strptime(start,FORMAT),datetime.strptime(end,FORMAT)
    seconds=int((b-a).total_seconds())
    if seconds<2:raise SyncProblem('НК ограничил даже секундный интервал. Нужна выгрузка каталога через оператора; карточки не считаются полностью синхронизированными.')
    from datetime import timedelta
    middle=(a+timedelta(seconds=seconds//2)).strftime(FORMAT)
    return {**body,'windows':[[start,middle,0],[middle,end,0],*body['windows'][1:]]}
