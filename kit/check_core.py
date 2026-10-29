#!/usr/bin/env python3
"""Contract smoke checks. Does not certify AI quality, durability or all security properties."""
import argparse,concurrent.futures,json,time,uuid
from client import request,wait_run

def main():
    p=argparse.ArgumentParser();p.add_argument('--api',default='http://localhost:8090');p.add_argument('--kit',default='http://localhost:8091');a=p.parse_args();checks=[]
    atlas='atlas-reviewer-key';boreal='boreal-reviewer-key';reader='atlas-reader-key'
    def check(name,fn):
        try:fn();checks.append({'check':name,'pass':True});print('PASS',name)
        except Exception as e:checks.append({'check':name,'pass':False,'detail':str(e)});print('FAIL',name,str(e))
    def eq(v,w):assert v==w, f'{v!r} != {w!r}'
    def audit():return request(a.kit,'/__admin/audit',token='kit-admin-token')[1]
    body={'request_id':'smoke-'+uuid.uuid4().hex,'message':'Please check my held payout and request review.','payment_id':'pay_held'}
    code,created=request(a.api,'/v1/runs','POST',body,atlas)
    if code not in [200,202] or 'run_id' not in created:raise SystemExit('Create run failed: '+str((code,created)))
    rid=created['run_id']
    check('unknown token rejected',lambda:eq(request(a.api,'/v1/runs','POST',body,'invalid-key')[0],401))
    check('same request is idempotent',lambda:eq(request(a.api,'/v1/runs','POST',body,atlas)[1]['run_id'],rid))
    check('conflicting replay is 409',lambda:eq(request(a.api,'/v1/runs','POST',{**body,'message':'different'},atlas)[0],409))
    check('cross-tenant read is 404',lambda:eq(request(a.api,'/v1/runs/'+rid,token=boreal)[0],404))
    run=wait_run(a.api,rid,atlas)
    check('held case succeeds',lambda:eq(run.get('status'),'succeeded'))
    act=run.get('action') or {};aid=act.get('action_id');ph=act.get('payload_hash')
    check('action waits for approval',lambda:eq(act.get('status'),'pending_approval'))
    check('no unapproved side effect',lambda:eq(len([r for r in audit()['receipts'] if r['action_id']==aid]),0))
    approval={'action_id':aid,'payload_hash':ph}
    check('reader cannot approve',lambda:eq(request(a.api,'/v1/runs/'+rid+'/approve','POST',approval,reader)[0],403))
    check('changed approval hash rejected',lambda:eq(request(a.api,'/v1/runs/'+rid+'/approve','POST',{**approval,'payload_hash':'0'*64},atlas)[0],409))
    # Inject external commit with lost response; the application's worker must retry/reconcile the stable key.
    request(a.kit,'/__admin/config','POST',{'write_fault':'commit_then_disconnect'},'kit-admin-token')
    def approve(_):return request(a.api,'/v1/runs/'+rid+'/approve','POST',approval,atlas)[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:codes=list(ex.map(approve,range(12)))
    check('approval retries accepted',lambda:all_codes(codes))
    end=time.monotonic()+75;last={}
    while time.monotonic()<end:
        _,last=request(a.api,'/v1/runs/'+rid,token=atlas)
        if (last.get('action') or {}).get('status') in ['completed','failed']:break
        time.sleep(.2)
    check('ambiguous write recovers',lambda:eq((last.get('action') or {}).get('status'),'completed'))
    check('one external effect',lambda:eq(len([r for r in audit()['receipts'] if r['action_id']==aid]),1))
    check('trace is tenant-scoped',lambda:eq(request(a.api,'/v1/runs/'+rid+'/trace',token=boreal)[0],404))
    def trace_safe():
        code,tr=request(a.api,'/v1/runs/'+rid+'/trace',token=atlas);eq(code,200);raw=json.dumps(tr)
        for secret in ['kit-model-token','kit-tool-token','kit-admin-token','atlas-reviewer-key','BOREAL-ONLY-9263']:assert secret not in raw,'leaked '+secret
        assert isinstance(tr.get('events'),list),'missing event list'
    check('trace is present and redacted',trace_safe)
    print(json.dumps({'checks':checks,'passed':sum(x['pass'] for x in checks),'total':len(checks)},indent=2));return 0 if all(x['pass'] for x in checks) else 1

def all_codes(codes):
    assert all(c in [200,202] for c in codes),codes
if __name__=='__main__':raise SystemExit(main())
