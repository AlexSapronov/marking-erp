class RemoteError(ValueError):
    """Sanitized error; request URLs and credentials are never included."""
    def __init__(self,message,*,status=None,retry_after=None):
        super().__init__(message);self.status=status;self.retry_after=retry_after


def report_problem(result):
    """Map a known rejection to guidance, without echoing remote codes or secrets."""
    def texts(value,depth=0):
        if depth>12:return []
        if isinstance(value,str):return [value[:8192].casefold()]
        if isinstance(value,dict):return [s for v in list(value.values())[:200] for s in texts(v,depth+1)]
        if isinstance(value,list):return [s for v in value[:200] for s in texts(v,depth+1)]
        return []
    if any('карточк' in text and ('не найдена' in text or 'нет прав' in text) for text in texts(result)):
        return ('catalog_not_ready','Карточка в НК ещё не готова или недоступна. Попробуйте сформировать отчёт позже или свяжитесь с ответственным за каталог.')
    return ('report_rejected','ЧЗ отклонил отчёт. Проверьте квитанцию с ответственным за маркировку и повторите отправку после исправления причины.')
