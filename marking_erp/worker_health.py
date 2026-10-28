"""Local evidence, not an external probe or permission to retry a mutation."""
import re,threading
from uuid import uuid4
from sqlalchemy import text

HEARTBEAT_SECONDS=5
STALE_SECONDS=20
STAGE_LIMITS={'starting':30,'idle':30,'cycle':300,'schedule':120,'auto_print':180,
              'dispatch':180,'reconcile':180,'catalog_sync':180,'pipeline':180,'token_refresh':180,'stopped':0}


class WorkerObserver:
    def __init__(self,engine,build_sha):
        self.engine=engine;self.id=uuid4();self.build=build_sha if re.fullmatch('[a-f0-9]{40}',build_sha or '') else None
        self.stop=threading.Event();self.thread=None;self.registered=False;self.alive=lambda:False

    def write(self,fn):
        # Monitoring must not break business work, emit secrets, or wait on a
        # business document lock. A DB outage produces missing/stale evidence.
        try:
            with self.engine.begin() as c:
                c.execute(text("SET LOCAL statement_timeout='1000ms'"))
                c.execute(text("SET LOCAL lock_timeout='500ms'"))
                fn(c)
            return True
        except Exception:return False

    def start(self,alive):
        self.alive=alive
        self.registered=self.write(self.register)
        self.thread=threading.Thread(target=self.pulse,daemon=True,name='worker-heartbeat');self.thread.start()

    def register(self,c):
        inserted=c.execute(text("INSERT INTO worker_runs(id,build_sha,state,stage,heartbeat_at) VALUES (:id,:build,'starting','starting',clock_timestamp()) ON CONFLICT DO NOTHING RETURNING id"),{'id':self.id,'build':self.build}).scalar_one_or_none()
        if inserted:self.event(c,'started')

    def event(self,c,action):
        c.execute(text('INSERT INTO worker_events(worker_id,action) VALUES (:id,:action)'),{'id':self.id,'action':action})

    def pulse(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            if not self.registered:self.registered=self.write(self.register)
            self.write(lambda c:c.execute(text("UPDATE worker_runs SET heartbeat_at=clock_timestamp(),thread_alive=:alive WHERE id=:id AND state NOT IN ('stopped','restored')"),{'id':self.id,'alive':bool(self.alive())}))

    def phase(self,stage):
        if not self.registered:return
        if stage not in STAGE_LIMITS:raise ValueError('worker_stage')
        self.write(lambda c:c.execute(text("UPDATE worker_runs SET stage=:stage,stage_started_at=clock_timestamp(),state='running',thread_alive=true WHERE id=:id AND state NOT IN ('stopped','restored')"),{'id':self.id,'stage':stage}))

    def begin(self):
        if not self.registered:return
        self.write(lambda c:c.execute(text("UPDATE worker_runs SET cycle_started_at=clock_timestamp(),stage_started_at=clock_timestamp(),stage='cycle',state='running',thread_alive=true WHERE id=:id AND state NOT IN ('stopped','restored')"),{'id':self.id}))

    def finish(self,success):
        if not self.registered:return
        def update(c):
            row=c.execute(text("SELECT consecutive_errors FROM worker_runs WHERE id=:id AND state NOT IN ('stopped','restored') FOR UPDATE"),{'id':self.id}).mappings().first()
            if not row:return
            if success is None:
                c.execute(text("UPDATE worker_runs SET state='idle',stage='idle',stage_started_at=clock_timestamp() WHERE id=:id"),{'id':self.id})
            elif success:
                c.execute(text("UPDATE worker_runs SET state='idle',stage='idle',stage_started_at=clock_timestamp(),last_success_at=clock_timestamp(),cycles=cycles+1,consecutive_errors=0 WHERE id=:id"),{'id':self.id})
                if row['consecutive_errors']:self.event(c,'recovered')
            else:
                c.execute(text("UPDATE worker_runs SET state='error',last_error_at=clock_timestamp(),errors=errors+1,consecutive_errors=consecutive_errors+1 WHERE id=:id"),{'id':self.id})
                # One event per failure episode; the counter tracks every cycle.
                if not row['consecutive_errors']:self.event(c,'cycle_failed')
        self.write(update)

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=4)
        if not self.registered:return
        def update(c):
            changed=c.execute(text("UPDATE worker_runs SET state='stopped',stage='stopped',thread_alive=false,stopped_at=clock_timestamp() WHERE id=:id AND state NOT IN ('stopped','restored') RETURNING id"),{'id':self.id}).scalar_one_or_none()
            if changed:self.event(c,'stopped')
        self.write(update)


def workers(c):
    now=c.execute(text('SELECT clock_timestamp()')).scalar_one()
    rows=c.execute(text('''SELECT id,build_sha,started_at,heartbeat_at,thread_alive,state,stage,stage_started_at,
        cycle_started_at,last_success_at,last_error_at,cycles,errors,consecutive_errors,stopped_at
        FROM worker_runs ORDER BY (state NOT IN ('stopped','restored') AND thread_alive AND heartbeat_at>clock_timestamp()-interval '20 seconds') DESC NULLS LAST,started_at DESC,id DESC LIMIT 21''')).mappings().all()
    instances=[];fresh=0;delayed=0;failed=0;starting=0
    for r in rows[:20]:
        age=(now-r['heartbeat_at']).total_seconds() if r['heartbeat_at'] else None
        stage_age=max(0,(now-r['stage_started_at']).total_seconds())
        signal=age is not None and 0<=age<=STALE_SECONDS and r['state'] not in ('stopped','restored')
        live=signal and r['thread_alive']
        long=live and stage_age>STAGE_LIMITS[r['stage']]
        is_starting=signal and r['state']=='starting' and stage_age<=30
        fresh+=bool(live);delayed+=bool(long);failed+=bool(live and r['consecutive_errors']);starting+=bool(is_starting)
        observed='historical' if r['state'] in ('stopped','restored') else 'starting' if is_starting else 'stale' if not live else 'error' if r['consecutive_errors'] else 'delayed' if long else 'active'
        instances.append({**dict(r),'observation':observed,'stage_age_seconds':int(stage_age),'stage_limit_seconds':STAGE_LIMITS[r['stage']]})
    events=[dict(r) for r in c.execute(text('SELECT action,created_at FROM worker_events ORDER BY id DESC LIMIT 20')).mappings()]
    return {'observed':bool(rows),'truncated':len(rows)>20,'available':fresh>0,'active':fresh,'delayed':delayed,'failing':failed,'starting':starting,
        'heartbeat_seconds':HEARTBEAT_SECONDS,'stale_after_seconds':STALE_SECONDS,'instances':instances,'events':events}


def queue_alerts(c):
    """Aggregates only; no payloads, business IDs, raw errors or state changes."""
    queries=[
        ('unknown_operations','critical',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM external_operations WHERE state='unknown'"),
        ('blocked_operations','warning',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM external_operations WHERE state='blocked'"),
        ('ready_queue_delayed','warning',"SELECT count(*) AS count,min(available_at) AS oldest_at FROM external_operations WHERE state='queued' AND available_at<now()-interval '5 minutes'"),
        ('dispatch_lease_expired','critical',"SELECT count(*) AS count,min(COALESCE(lease_until,updated_at)) AS oldest_at FROM external_operations WHERE state='dispatching' AND (lease_until IS NULL OR lease_until<now())"),
        ('external_receipt_delayed','warning',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM external_operations WHERE state='submitted' AND updated_at<now()-interval '10 minutes'"),
        ('signature_waiting','warning',"SELECT count(*) AS count,min(created_at) AS oldest_at FROM external_operations WHERE state='awaiting_signature' AND created_at<now()-interval '5 minutes'"),
        ('print_unknown','critical',"SELECT count(*) AS count,min(created_at) AS oldest_at FROM print_jobs WHERE (state='unknown' OR (state='claimed' AND (claimed_at IS NULL OR claimed_at<now()-interval '5 minutes')))"),
        ('print_queue_delayed','warning',"SELECT count(*) AS count,min(created_at) AS oldest_at FROM print_jobs WHERE state='queued' AND created_at<now()-interval '5 minutes'"),
        ('print_held','warning',"SELECT count(*) AS count,min(created_at) AS oldest_at FROM print_jobs WHERE state='held'"),
        ('catalog_processing_delayed','warning',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM business_items WHERE (kind='sync' OR (kind='import' AND body->>'pipeline'='true')) AND state='processing' AND updated_at<now()-interval '10 minutes'"),
        ('catalog_blocked','warning',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM business_items WHERE kind IN ('sync','import') AND state='blocked'"),
        ('authorization_expired','warning',"SELECT count(*) AS count,min(expires_at) AS oldest_at FROM integration_auth_jobs WHERE state IN ('pending','leased') AND expires_at<now()"),
        ('auto_print_blocked','warning',"SELECT count(*) AS count,min(updated_at) AS oldest_at FROM auto_print_requests WHERE state='blocked'"),
        ('auto_print_ready_delayed','warning',"SELECT count(*) AS count,min(r.updated_at) AS oldest_at FROM auto_print_requests r JOIN business_items i ON i.id=r.item_id WHERE r.state='waiting' AND NULLIF(i.body->>'label_file_id','') IS NOT NULL AND r.updated_at<now()-interval '5 minutes'")]
    result=[]
    for code,severity,query in queries:
        org='r.organization_id' if code=='auto_print_ready_delayed' else 'organization_id'
        row=c.execute(text(query+f' AND {org} IN (SELECT id FROM organizations WHERE enabled)')).mappings().one()
        if row['count']:result.append({'code':code,'severity':severity,**dict(row)})
    return result


def health(c):
    status=workers(c);alerts=queue_alerts(c)
    if not status['available'] and not status['starting']:alerts.insert(0,{'code':'worker_unavailable','severity':'critical','count':1,'oldest_at':None})
    if status['delayed']:alerts.insert(0,{'code':'worker_delayed','severity':'warning','count':status['delayed'],'oldest_at':None})
    if status['failing']:alerts.insert(0,{'code':'worker_errors','severity':'warning','count':status['failing'],'oldest_at':None})
    return {'workers':status,'alerts':alerts,'worker_liveness_verified':status['available']}
