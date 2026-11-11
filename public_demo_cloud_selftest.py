"""Owner-triggered, persisted once-only actual PG checks. Never auto retry.
Synthetic visitor tokens only: not a real public browser or judge login test.
"""
import json
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from backend import APIError
from public_demo_postgres import REG,registry_connection

def ensure(value):
    if not value:raise AssertionError('QA check failed')

def denied(fn):
    try:fn()
    except APIError:return
    raise AssertionError('Expected denial')

QA_PROTOCOL='aurora-public-pg-qa-v2'
QA_FILES=('public_demo_cloud_selftest.py','public_demo_postgres.py','public_demo_cloud.py',
          'public_demo.py','backend.py','persistence.py','supply_alternatives.py','public_ai.py','public_ai_selftest.py')

def qa_version():
    # Exact code identity, not a self-declared passed flag. Profile release/window
    # toggles are excluded; app/DB binding is independently enforced by registry.
    digest=hashlib.sha256(QA_PROTOCOL.encode())
    for name in QA_FILES:
        digest.update(name.encode()+b'\0'+Path(__file__).with_name(name).read_bytes())
    return QA_PROTOCOL+':'+digest.hexdigest()

def decode_result(row,version):
    if not row:return {'status':'awaiting_owner_qa','qa_version':version,'real_judge_login_tested':False}
    status,encoded=row
    if status not in ('running','passed','failed'):
        return {'status':'failed','reason':'invalid_persisted_qa_status','qa_version':version,'real_judge_login_tested':False}
    if not encoded:
        return {'status':status if status!='passed' else 'failed','qa_version':version,'real_judge_login_tested':False}
    try:result=json.loads(encoded)
    except (ValueError,TypeError):result={}
    if not isinstance(result,dict) or result.get('qa_version')!=version or result.get('status')!=status:
        return {'status':'failed','reason':'persisted_qa_version_mismatch','qa_version':version,'real_judge_login_tested':False}
    return result

def load_result(reg):
    """Read-only restart recovery. Never executes tests or upgrades legacy QA."""
    version=qa_version()
    with registry_connection(reg.config,reg.connector) as db:
        if not db.execute('SELECT to_regclass(%s)',(REG+'.qa_versions',)).fetchone()[0]:
            return decode_result(None,version)
        row=db.execute(f'SELECT status,result FROM {REG}.qa_versions WHERE version=%s',(version,)).fetchone()
        return decode_result(row,version)

def run_once(reg):
    version=qa_version()
    with registry_connection(reg.config,reg.connector) as db:
        # Legacy qa(id=1) remains untouched, including its failed/passed evidence.
        db.execute(f'CREATE TABLE IF NOT EXISTS {REG}.qa_versions(version TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT)')
        old=db.execute(f'SELECT status,result FROM {REG}.qa_versions WHERE version=%s',(version,)).fetchone()
        if old:return decode_result(old,version)
        db.execute(f"INSERT INTO {REG}.qa_versions VALUES(%s,'running',NULL)",(version,))
    checks=[]
    def check(name,fn):
        try:fn();checks.append({'name':name,'status':'pass'})
        except Exception as error:checks.append({'name':name,'status':'fail','error_type':type(error).__name__})
    try:
        a,ca=reg.resolve(None);b,cb=reg.resolve(None)
        check('separate_seeded_state',lambda:ensure(a.workspace!=b.workspace and len(a.state()['cases'])==3 and len(b.state()['cases'])==3))
        def sessions():
            token=a.session('quality')['token'];ensure(a.authenticate(token)=='quality');denied(lambda:b.authenticate(token));denied(lambda:reg.resolve('x'*43))
        check('role_and_forged_cookie_denial',sessions)
        def approve():
            a.mutate('/api/cases/CASE-001/submit',{'expected_version':1},'quality','qa-submit')
            a.mutate('/api/cases/CASE-001/decision',{'expected_version':a.detail('CASE-001')['version'],'decision':'approve','reason':'Synthetic QA only'},'committee','qa-approve')
            ensure(a.detail('CASE-001')['recall_authorized']);ensure(b.detail('CASE-001')['version']==1)
        check('normal_approval_isolated',approve)
        def change():
            a.mutate('/api/cases/CASE-001/conditions',{'expected_version':a.detail('CASE-001')['version'],'changes':{'production_delay_days':4},'reason':'Synthetic QA delay'},'production','qa-change')
            ensure(a.detail('CASE-001')['conditions']['production_delay_days']==4)
        check('changed_conditions',change)
        def reject():
            b.mutate('/api/cases/CASE-001/submit',{'expected_version':1},'quality','qa-submit')
            b.mutate('/api/cases/CASE-001/decision',{'expected_version':b.detail('CASE-001')['version'],'decision':'reject','reason':'Synthetic QA rejection'},'committee','qa-reject')
            ensure(b.detail('CASE-001')['status']=='rejected')
            ensure(any(t['type']=='committee_recheck' for t in b.detail('CASE-001')['tasks']))
        check('rejection_followup',reject)
        def audits():
            ensure(a.verify_audit()['valid'] and b.verify_audit()['valid'])
            with a.connect() as db:ac=db.execute('SELECT count(*) FROM idempotency').fetchone()[0]
            with b.connect() as db:bc=db.execute('SELECT count(*) FROM idempotency').fetchone()[0]
            ensure(ac!=bc)
        check('audit_and_idempotency_scoped',audits)
        def concurrent_scope():
            def read(service):
                for _ in range(3):
                    with service.connect() as db:
                        ensure(db.execute('SELECT current_schema()').fetchone()[0]=='apd_'+service.workspace)
                        db.commit()
                        ensure(db.execute('SELECT current_schema()').fetchone()[0]=='apd_'+service.workspace)
            with ThreadPoolExecutor(2) as pool:list(pool.map(read,[a,b]))
        check('concurrent_scope_commit_reset',concurrent_scope)
        limited,token=reg.resolve(None)
        def requests():
            with registry_connection(reg.config,reg.connector) as db:db.execute(f'UPDATE {REG}.workspaces SET request_cap=2 WHERE id=%s',(limited.workspace,))
            def attempt(_):
                try:reg.resolve(token);return 1
                except APIError as error:ensure(error.code=='DEMO_REQUEST_LIMIT');return 0
            with ThreadPoolExecutor(3) as pool:ensure(sum(pool.map(attempt,range(3)))==1)
        check('concurrent_request_limit',requests)
        def expiry():
            with registry_connection(reg.config,reg.connector) as db:db.execute(f'UPDATE {REG}.workspaces SET expires=0 WHERE id=%s',(limited.workspace,))
            denied(lambda:reg.resolve(token));denied(limited.state)
        check('persisted_expiry_cached_service',expiry)
        def offline():ensure(not a.state()['ai']['request_allowed']);denied(lambda:a.ai.generate({}))
        check('ai_off',offline)
        from public_ai_selftest import run_public_ai_checks
        checks.extend(run_public_ai_checks(reg))
    except Exception as error:checks.append({'name':'setup','status':'fail','error_type':type(error).__name__})
    result={'qa_version':version,'status':'passed' if len(checks)==15 and all(c['status']=='pass' for c in checks) else 'failed','checks':checks,'real_postgresql':True,'real_judge_login_tested':False,'real_public_browser_tested':False,'ai_calls':0}
    with registry_connection(reg.config,reg.connector) as db:db.execute(f'UPDATE {REG}.qa_versions SET status=%s,result=%s WHERE version=%s',(result['status'],json.dumps(result),version))
    return result
