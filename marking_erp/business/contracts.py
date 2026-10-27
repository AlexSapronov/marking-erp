"""Explicit adapters ported from the source applications; no arbitrary endpoints."""
from sqlalchemy import text
CONTRACTS={
 'nk.gtin':('nk.gtin.generate',False),'nk.save':('nk.cards.edit',False),'nk.moderate':('nk.cards.moderate',False),
 'nk.document':('nk.cards.publish',False),'nk.publish':('nk.cards.publish',True),
 'bm.order':('marking.codes.issue',True),'bm.fetch':('marking.codes.issue',False),
 'bm.report':('marking.reports.submit',True),'bm.atk':('marking.atk.create',True),
 'bm.transform':('marking.atk.transform.approve',True),
 'split.order':('split.operations.create',True),'split.fetch':('split.operations.create',False),
 'split.report':('split.reports.submit',True),
 'remark.order':('labels.remarking.create',True),'remark.fetch':('labels.remarking.create',False),
 'remark.report':('labels.remarking.submit',True),'remark.submit':('labels.remarking.submit',True),
 'fts.submit':('logistics.fts.submit',True),
}
VERSION='source-adapters-20261026-v1'

def register(c):
    for kind,(permission,signed) in CONTRACTS.items():
        c.execute(text('''INSERT INTO operation_contracts(kind,permission_id,version,semantic,requires_signature,approved,evidence_ref)
          VALUES (:kind,:permission,:version,'mutation',:signed,true,:ref)
          ON CONFLICT(kind) DO NOTHING'''),{'kind':kind,'permission':permission,'version':VERSION,'signed':signed,'ref':('https://docs.crpt.ru/gismt/True_API/ v747 §§4.1.4.8,4.1.19' if kind in ('fts.submit','remark.submit') or kind.startswith('remark.') else 'nk-manager@36efc06a38ec4e06fd6fcc21307bc397f9258135' if kind.startswith('nk.') else 'km-split@19c8329a2f9c4298853939c7d68a91443cf98bf3' if kind.startswith('split.') else 'marking-bm@6a55adb9b6ce50872141416322fd10bc8d790a45')+'; deployment pilot pending; docs/BUSINESS-MVP.md'})

    for service in ('true','oms'):
        c.execute(text("""INSERT INTO operation_contracts(kind,permission_id,version,semantic,requires_signature,approved,evidence_ref)
            VALUES (:kind,'integrations.manage',:version,'mutation',true,true,:ref) ON CONFLICT(kind) DO NOTHING"""),{'kind':'auth.'+service,'version':VERSION,'ref':'https://docs.crpt.ru/gismt/True_API/ v747 §§1.5,11.1; trusted delegated renewal only'})
