"""Loopback-only evidence transport for resuming a pinned validator runtime.

Forwards bytes unchanged to one trusted backend. It never runs models, changes
scientific policy, extends deadlines, or handles worker claims and awards.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import unquote,urlsplit
import httpx

PREFIX='/internal/competition/scientific/'


def allowed(method,path,round_id,run_id):
    parsed=urlsplit(path)
    if parsed.query or parsed.fragment or parsed.path!=path or not path.startswith(PREFIX):return False
    rest=path[len(PREFIX):]
    if re.fullmatch(r'(blobs|source-references)/[0-9a-f]{64}',rest):
        return method=='GET' or method=='PUT' and rest.startswith('blobs/')
    if rest.startswith('heads/'):
        key=unquote(rest[6:])
        return method in {'GET','POST'} and key.startswith(('recovery:'+run_id+':','model-operation:','judge-run:','provider-usage:','model-completion:','billing-reconciliation:','completion-publication-repair:'))
    if rest=='recoveries/'+round_id+'/'+run_id:return method=='GET'
    if rest.startswith('recoveries/'+round_id+'/'+run_id+'/papers/'):
        tail=rest.split('/papers/',1)[1].split('/')
        return method=='POST' and len(tail)==2 and all(re.fullmatch(r'[A-Za-z0-9_-]+',p) for p in tail)
    if rest.startswith('sources/'+round_id+'/'):return method=='GET'
    return False


class Relay:
    def __init__(self,client,round_id,run_id):
        self.client=client;self.round_id=round_id;self.run_id=run_id
        self.confirmed={};self.lock=threading.Lock();self.requests=0;self.retries=0
    def forward(self,method,path,body,authorization):
        if not allowed(method,path,self.round_id,self.run_id):return 403,b'{"error":"outside evidence scope"}','application/json'
        if method=='PUT':
            digest=path.rsplit('/',1)[1]
            if hashlib.sha256(body).hexdigest()!=digest:return 400,b'{"error":"hash mismatch"}','application/json'
            with self.lock:cached=self.confirmed.get(digest)
            if cached is not None:return cached
        for attempt in range(3):
            with self.lock:self.requests+=1
            try:
                response=self.client.request(method,path,content=body,headers={'Authorization':authorization,'Content-Type':'application/octet-stream' if method=='PUT' else 'application/json'})
                result=(response.status_code,response.content,response.headers.get('content-type','application/octet-stream'))
                retry=response.status_code in {408,429,500,502,503,504}
            except httpx.TransportError:
                result=(502,b'{"error":"evidence upstream unavailable"}','application/json');retry=True
            if not retry or attempt==2:break
            with self.lock:self.retries+=1
            time.sleep(2**attempt)
        if method=='PUT' and 200<=result[0]<300:
            try:receipt=json.loads(result[1])
            except ValueError:return 502,b'{"error":"invalid evidence receipt"}','application/json'
            if receipt!={'sha256':digest,'byte_length':len(body)}:return 502,b'{"error":"evidence receipt mismatch"}','application/json'
            with self.lock:self.confirmed[digest]=result
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,required=True);parser.add_argument('--upstream',choices=['https://api.claims111.ai','https://claims-api-dev.vercel.app'],required=True)
    parser.add_argument('--round-id',required=True);parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    if not 1024<=args.port<=65535 or not re.fullmatch(r'round_[A-Za-z0-9_-]+_testnet',args.round_id) or not re.fullmatch(r'[0-9a-f]{32}',args.run_id):parser.error('Invalid testnet evidence relay scope')
    token=os.environ['CLAIMS_COMPETITION_CONTROLLER_TOKEN'];authorization='Bearer '+token
    client=httpx.Client(base_url=args.upstream,timeout=httpx.Timeout(90,connect=10),limits=httpx.Limits(max_connections=20,max_keepalive_connections=10),trust_env=False)
    relay=Relay(client,args.round_id,args.run_id)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def dispatch(self):
            if self.command=='GET' and self.path=='/healthz':
                self.reply(200,json.dumps({'requests':relay.requests,'retries':relay.retries,'confirmed_blobs':len(relay.confirmed)}).encode(),'application/json');return
            supplied=self.headers.get('Authorization','')
            if not hmac.compare_digest(supplied.encode(),authorization.encode()):self.reply(401,b'{"error":"unauthorized"}','application/json');return
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:self.reply(400,b'','application/json');return
            if not 0<=length<=210000000:self.reply(413,b'','application/json');return
            if not allowed(self.command,self.path,args.round_id,args.run_id):self.reply(403,b'','application/json');return
            result=relay.forward(self.command,self.path,self.rfile.read(length),supplied)
            self.reply(*result)
        def reply(self,status,body,content_type):
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        do_GET=dispatch;do_POST=dispatch;do_PUT=dispatch
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(json.dumps({'status':'listening','round_id':args.round_id,'run_id':args.run_id,'port':args.port}),flush=True)
    try:server.serve_forever()
    finally:server.server_close();client.close()


if __name__=='__main__':main()
