"""Effective NK rules; no similarity rule ever substitutes a product identity."""
from ..policy import snapshot


def rules(c, org):
    return snapshot(c, org)


def duplicate_filter(c, org, value):
    if value is not None:
        return value
    return 'hide' if rules(c, org)['values']['nk.drafts.hide_published_duplicates'] else 'include'
