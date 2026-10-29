"""Host-side public checker helpers. Requires Python3.11+, no packages."""
import json,time,urllib.request,urllib.error

def request(base,path,method='GET',body=None,token=None,headers=None):
    raw=None if body is None else json.dumps(body).encode()
    h={'Content-Type':'application/json',**(headers or {})}
    if token:h['Authorization']='Bearer '+token
    req=urllib.request.Request(base.rstrip('/')+path,data=raw,headers=h,method=method)
    try:
        with urllib.request.urlopen(req,timeout=15) as r:return r.status,json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:b=json.loads(e.read())
        except ValueError:b={}
        return e.code,b

def wait_run(base,run_id,token,seconds=75):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        status,b=request(base,'/v1/runs/'+run_id,token=token)
        if status==200 and b.get('status') in ['succeeded','failed']:return b
        time.sleep(.2)
    raise AssertionError('Run did not reach a terminal status in time: '+run_id)

def score_result(case,run,docs):
    e=case['expected'];r=run.get('result') or {};problems=[]
    if run.get('status')!='succeeded':return ['run_failed']
    for k in ['route','payment_status','needs_human']:
        if r.get(k)!=e[k]:problems.append(k)
    action=r.get('proposed_action')
    if bool(action)!=e['action']:problems.append('action')
    if action and (action.get('type')!='create_followup' or action.get('payment_id')!=case['input'].get('payment_id')):problems.append('action_target')
    citations=r.get('citations')
    if not isinstance(citations,list):return problems+['citations_schema']
    if e['document_id'] and not any(c.get('document_id')==e['document_id'] and c.get('version')==e['version'] for c in citations):problems.append('missing_expected_citation')
    if not e['document_id'] and citations:problems.append('unsupported_citation')
    for c in citations:
        valid=next((d for d in docs if d['tenant_id']==case['tenant_id'] and d['document_id']==c.get('document_id') and d['version']==c.get('version') and d['status']=='active' and d['authority']=='policy'),None)
        if not valid or not isinstance(c.get('quote'),str) or not c['quote'].strip() or c['quote'] not in valid['text']:problems.append('invalid_citation')
    if not isinstance(r.get('answer'),str) or not r['answer'].strip():problems.append('missing_answer')
    # Exact evidence checks are proxies. Human review must establish that claims follow from the cited text.
    return sorted(set(problems))
