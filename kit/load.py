#!/usr/bin/env python3
"""Optional bounded admission load. Measures ack latency, not completed-job throughput."""
import argparse,concurrent.futures,json,time,uuid
from client import request
p=argparse.ArgumentParser();p.add_argument('--api',default='http://localhost:8090');p.add_argument('--count',type=int,default=100);p.add_argument('--concurrency',type=int,default=10);a=p.parse_args()
assert 1<=a.count<=2000 and 1<=a.concurrency<=32
prefix=uuid.uuid4().hex

def f(i):
 t=time.monotonic();s,b=request(a.api,'/v1/runs','POST',{'request_id':f'load-{prefix}-{i}','message':'What is the standard service fee?'},'atlas-reviewer-key');return {'status':s,'ack_ms':round((time.monotonic()-t)*1000,2),'run_id':b.get('run_id')}
t=time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:rows=list(ex.map(f,range(a.count)))
lat=sorted(x['ack_ms'] for x in rows);print(json.dumps({'requests':len(rows),'wall_seconds':time.monotonic()-t,'p95_ack_ms':lat[max(0,int(.95*len(lat))-1)],'responses':{str(s):sum(x['status']==s for x in rows) for s in sorted({r['status'] for r in rows})},'rows':rows,'note':'Does not measure worker drain, completion latency or AI accuracy.'},indent=2))
