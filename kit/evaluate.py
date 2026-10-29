#!/usr/bin/env python3
import argparse,json,time,uuid
from pathlib import Path
from client import request,wait_run,score_result

def main():
    p=argparse.ArgumentParser();p.add_argument('--api',default='http://localhost:8090');p.add_argument('--cases',default=str(Path(__file__).parent/'data/dev.jsonl'));p.add_argument('--documents',default=str(Path(__file__).parent/'data/documents.json'));p.add_argument('--out',default='results/evaluation.jsonl');a=p.parse_args()
    cases=[json.loads(x) for x in Path(a.cases).read_text().splitlines() if x.strip()];docs=json.loads(Path(a.documents).read_text());rows=[]
    for c in cases:
        token=c['tenant_id']+'-reviewer-key';start=time.monotonic()
        try:
            status,b=request(a.api,'/v1/runs','POST',{'request_id':'eval-'+uuid.uuid4().hex,**c['input']},token)
            if status not in [200,202]:raise AssertionError('Create returned '+str(status))
            run=wait_run(a.api,b['run_id'],token)
            problems=score_result(c,run,docs)
            row={'case_id':c['case_id'],'problems':problems,'passed':not problems,'seconds':round(time.monotonic()-start,3),'run':run}
        except Exception as e:row={'case_id':c['case_id'],'passed':False,'error':str(e),'seconds':round(time.monotonic()-start,3)}
        rows.append(row);print(c['case_id'],'PASS' if row['passed'] else 'FAIL',flush=True)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    print(json.dumps({'case_success':sum(x['passed'] for x in rows),'cases':len(rows),'note':'Proxy checks only. Read answers, evidence and tool traces for semantic correctness.'},indent=2))
    return 0 if all(x['passed'] for x in rows) else 1
if __name__=='__main__':raise SystemExit(main())
