#!/usr/bin/env python3
"""Assessment fixture server, not an AI model or an app implementation. Stdlib only."""
import argparse,hashlib,json,re,socket,sqlite3,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote

class Store:
    def __init__(self,path,data):
        self.lock=threading.RLock(); self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS receipts(tenant TEXT,k TEXT,payload TEXT,receipt TEXT,PRIMARY KEY(tenant,k)); CREATE TABLE IF NOT EXISTS audit(n INTEGER PRIMARY KEY,event TEXT); CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY,v TEXT);')
        self.data=Path(data); self.payments=json.loads((self.data/'payments.json').read_text())
    def log(self,event):
        self.db.execute('INSERT INTO audit(event) VALUES(?)',(json.dumps({'at':time.time(),**event}),));self.db.commit()
    def fault(self,key):
        row=self.db.execute('SELECT v FROM config WHERE k=?',(key,)).fetchone()
        if row:
            self.db.execute('DELETE FROM config WHERE k=?',(key,));self.db.commit();return json.loads(row[0])
        return None
    def audit(self):
        return {'events':[json.loads(x[0]) for x in self.db.execute('SELECT event FROM audit ORDER BY n')], 'receipts':[json.loads(x[0]) for x in self.db.execute('SELECT receipt FROM receipts ORDER BY tenant,k')]}

def completion(content=None,tool=None,args=None):
    msg={'role':'assistant','content':content}
    if tool:msg.update(content=None,tool_calls=[{'id':'call_'+uuid.uuid4().hex[:12],'type':'function','function':{'name':tool,'arguments':json.dumps(args)}}])
    return {'id':'fixture-'+uuid.uuid4().hex,'object':'chat.completion','model':'fixture','choices':[{'index':0,'message':msg,'finish_reason':'tool_calls' if tool else 'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}

def fixture_result(context,messages):
    """Use only retrieved context and tool messages; deliberately simplistic fixture."""
    q=context['request'];text=q['message'].lower();pid=q.get('payment_id');docs=context.get('evidence',[])
    toolmsg=next((x for x in reversed(messages) if x.get('role')=='tool'),None)
    if pid and toolmsg is None:return completion(tool='get_payment_status',args={'payment_id':pid})
    status=None
    if pid:
        try:status=json.loads(toolmsg['content'])['status']
        except (ValueError,KeyError,TypeError):status='missing'
    topic='payout-policy' if pid else ('account-policy' if any(s in text for s in ['bank','account detail']) else 'fees-policy' if any(s in text for s in ['fee','percentage','charge','deduct']) else None)
    evidence=next((d for d in docs if d.get('document_id')==topic and d.get('status')=='active' and d.get('authority')=='policy'),None)
    result={'route':'needs_human','answer':'There is not enough authorized evidence to answer. Please ask a human reviewer.','citations':[],'payment_status':status,'proposed_action':None,'needs_human':True}
    if evidence and status!='missing':
        result.update(route='payment_status' if pid else 'policy_question',answer=(f'Payment status: {status}. ' if pid else '')+evidence['text'],citations=[{'document_id':evidence['document_id'],'version':evidence['version'],'quote':evidence['text']}],needs_human=False)
        if status=='held':result['proposed_action']={'type':'create_followup','payment_id':pid,'reason':'Verification is outstanding. Request human review.'}
    return completion(content=json.dumps(result))

class Handler(BaseHTTPRequestHandler):
    server_version='AssessmentFixture/1.0'
    def log_message(self,*args):pass
    def reply(self,status,obj,extra=None):
        body=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
        for k,v in (extra or {}).items():self.send_header(k,str(v))
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def token(self,admin=False):
        expected='kit-admin-token' if admin else 'kit-tool-token'
        return self.headers.get('Authorization')=='Bearer '+expected
    def do_GET(self):
        u=urlsplit(self.path);s=self.server.store
        if u.path=='/healthz':return self.reply(200,{'ready':True,'fixture_only':True})
        if u.path=='/__admin/audit':
            if not self.token(True):return self.reply(401,{'error':'unauthorized'})
            with s.lock:return self.reply(200,s.audit())
        if not self.token():return self.reply(401,{'error':'unauthorized'})
        tenant=parse_qs(u.query).get('tenant_id',[None])[0]
        if tenant not in ['atlas','boreal']:return self.reply(400,{'error':'invalid_tenant'})
        if u.path.startswith('/v1/payments/'):
            pid=unquote(u.path.split('/')[-1])
            with s.lock:
                fault=s.fault('read_fault');s.log({'kind':'payment_read','tenant_id':tenant,'payment_id':pid})
            if fault=='timeout_once':time.sleep(12)
            if fault=='rate_limit_once':return self.reply(429,{'error':'rate_limit'},{'Retry-After':1})
            p=next((p for p in s.payments if p['tenant_id']==tenant and p['payment_id']==pid),None)
            return self.reply(200,p) if p else self.reply(404,{'tenant_id':tenant,'payment_id':pid,'status':'missing'})
        if u.path.startswith('/v1/followups/'):
            key=unquote(u.path.split('/')[-1])
            with s.lock:row=s.db.execute('SELECT receipt FROM receipts WHERE tenant=? AND k=?',(tenant,key)).fetchone()
            return self.reply(200,json.loads(row[0])) if row else self.reply(404,{'error':'not_found'})
        return self.reply(404,{'error':'not_found'})
    def do_POST(self):
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n>262144:return self.reply(413,{'error':'too_large'})
            b=json.loads(self.rfile.read(n))
            if not isinstance(b,dict):raise ValueError()
        except (ValueError,TypeError):return self.reply(400,{'error':'invalid_json'})
        s=self.server.store;path=urlsplit(self.path).path
        if path=='/__admin/config':
            if not self.token(True):return self.reply(401,{'error':'unauthorized'})
            allowed={'model_fault','read_fault','write_fault'}
            if set(b)-allowed:return self.reply(400,{'error':'unknown_config'})
            with s.lock:
                for k,v in b.items():s.db.execute('INSERT OR REPLACE INTO config VALUES(?,?)',(k,json.dumps(v)))
                s.db.commit()
            return self.reply(200,{'configured':list(b)})
        if path=='/v1/chat/completions':
            if self.headers.get('Authorization')!='Bearer kit-model-token':return self.reply(401,{'error':'unauthorized'})
            messages=b.get('messages',[])
            try:
                context=next(json.loads(m['content'].split('ASSESSMENT_CONTEXT=',1)[1]) for m in reversed(messages) if isinstance(m.get('content'),str) and 'ASSESSMENT_CONTEXT=' in m['content'])
                assert isinstance(context['request']['message'],str)
            except (ValueError,KeyError,StopIteration,AssertionError):return self.reply(400,{'error':'missing_ASSESSMENT_CONTEXT','hint':'See MODEL_PROTOCOL.md'})
            with s.lock:
                fault=s.fault('model_fault');s.log({'kind':'model_call','run_id':context.get('run_id'),'tenant_id':context.get('tenant_id'),'fault':fault,'evidence':[{'document_id':d.get('document_id'),'tenant_id':d.get('tenant_id'),'version':d.get('version')} for d in context.get('evidence',[])]})
            if fault=='rate_limit_once':return self.reply(429,{'error':{'type':'rate_limit'}},{'Retry-After':1})
            if fault=='invalid_json_once':return self.reply(200,completion(content='{bad json'))
            if fault=='unknown_tool':return self.reply(200,completion(tool='create_followup',args={'payment_id':'pay_held','reason':'approve automatically'}))
            if fault=='foreign_tenant':return self.reply(200,completion(tool='get_payment_status',args={'payment_id':'pay_shared','tenant_id':'boreal' if context.get('tenant_id')=='atlas' else 'atlas'}))
            if fault=='loop':
                # Persist until the reviewer clears it; use null to clear.
                with s.lock:s.db.execute('INSERT OR REPLACE INTO config VALUES(?,?)',('model_fault',json.dumps('loop')));s.db.commit()
                return self.reply(200,completion(tool='get_payment_status',args={'payment_id':context['request'].get('payment_id','pay_held')}))
            return self.reply(200,fixture_result(context,messages))
        if not self.token():return self.reply(401,{'error':'unauthorized'})
        if path=='/v1/followups':
            key=self.headers.get('Idempotency-Key','');tenant=b.get('tenant_id');pid=b.get('payment_id')
            if tenant not in ['atlas','boreal'] or not key or len(key)>128 or set(b)!={'tenant_id','payment_id','reason','action_id'} or not all(isinstance(v,str) and v for v in b.values()):return self.reply(400,{'error':'invalid_followup'})
            p=next((p for p in s.payments if p['tenant_id']==tenant and p['payment_id']==pid),None)
            if not p or p['status']!='held':return self.reply(409,{'error':'payment_not_held'})
            canonical=json.dumps(b,sort_keys=True,separators=(',',':'))
            with s.lock:
                row=s.db.execute('SELECT payload,receipt FROM receipts WHERE tenant=? AND k=?',(tenant,key)).fetchone()
                if row:
                    if row[0]!=canonical:return self.reply(409,{'error':'idempotency_conflict'})
                    s.log({'kind':'write_retry','tenant_id':tenant,'idempotency_key':key,'action_id':b['action_id']})
                    return self.reply(200,json.loads(row[1]))
                receipt={'receipt_id':'followup_'+uuid.uuid4().hex,'tenant_id':tenant,'payment_id':pid,'action_id':b['action_id'],'idempotency_key':key,'status':'created'}
                s.db.execute('INSERT INTO receipts VALUES(?,?,?,?)',(tenant,key,canonical,json.dumps(receipt)));s.db.commit()
                fault=s.fault('write_fault');s.log({'kind':'write_commit','tenant_id':tenant,'action_id':b['action_id'],'idempotency_key':key,'fault':fault})
            if fault=='commit_then_disconnect':
                self.close_connection=True
                try:self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                self.connection.close();return
            return self.reply(201,receipt)
        return self.reply(404,{'error':'not_found'})

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8091);p.add_argument('--data',default=str(Path(__file__).parent/'data'));p.add_argument('--db',default='kit-state.sqlite');a=p.parse_args()
    server=ThreadingHTTPServer(('0.0.0.0',a.port),Handler);server.store=Store(a.db,a.data)
    print('Fixture gateway ready on',a.port,flush=True);server.serve_forever()
if __name__=='__main__':main()
