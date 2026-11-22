"""Hash-checked remote evidence transport with local durable operation journal."""

import json
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import HTTPError
from .store import Store, Conflict, sha


class EvidenceTransportError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('Scientific evidence transport HTTP ' + str(status_code))


class RemoteStore(Store):
    def __init__(self, root, backend):
        self.backend = backend
        super().__init__(root)

    def _binary(self, method, path, body=None):
        req = Request(
            self.backend.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.backend.controller_token,
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urlopen(req, timeout=120) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 409:
                raise Conflict("Remote scientific predecessor changed") from exc
            raise EvidenceTransportError(exc.code) from exc

    def put_existing_source(self, round_id, paper_id, data):
        """Cache an already frozen paper locally; never relay PDF bytes via the API."""
        from .contracts import FileRef
        meta = self.backend.get_scientific_source(round_id, paper_id)
        digest = sha(data)
        if meta['source_sha256'] != digest:
            raise ValueError('PDF differs from the frozen source')
        Store.put_blob(self, digest, data)
        return FileRef(relative_path='sources/' + digest + '.pdf', sha256=digest,
                       byte_length=len(data), media_type='application/pdf')

    def _fetch_original(self, meta, expected):
        import os
        from competition.pdf_fetch import TrustedPdfFetcher
        from competition.hippius import hippius_s3_client
        if meta['source_sha256'] != expected:
            raise ValueError('Original source reference hash mismatch')
        buckets = tuple(x for x in os.environ.get('CLAIMS_COMPETITION_PDF_S3_BUCKETS', '').split(',') if x)
        endpoint = os.environ.get('CLAIMS_HIPPIUS_ENDPOINT', '')
        client = hippius_s3_client(endpoint=endpoint,
            access_key_id=os.environ['CLAIMS_HIPPIUS_ACCESS_KEY_ID'],
            secret_access_key=os.environ['CLAIMS_HIPPIUS_SECRET_ACCESS_KEY']) if buckets else None
        fetcher = TrustedPdfFetcher(
            allowed_https_hosts=tuple(x for x in os.environ.get('CLAIMS_COMPETITION_PDF_HTTPS_HOSTS', '').split(',') if x),
            allowed_s3_buckets=buckets, s3_client=client, hippius_endpoint=endpoint)
        return fetcher.fetch(meta['source_url'], expected)

    def put_blob(self, expected, data):
        # Local hash validation before sending bytes; remote also verifies independently.
        ref = super().put_blob(expected, data)
        result = json.loads(
            self._binary(
                "PUT", "/internal/competition/scientific/blobs/" + expected, data
            )
        )
        if result != {"sha256": expected, "byte_length": len(data)}:
            raise ValueError("Remote changed evidence receipt")
        return ref

    def get_blob(self, expected):
        try:
            return super().get_blob(expected)
        except KeyError:
            try:
                data = self._binary("GET", "/internal/competition/scientific/blobs/" + expected)
            except EvidenceTransportError as exc:
                if exc.status_code not in {404, 413, 502}:
                    raise
                meta = self.backend._request('GET', '/internal/competition/scientific/source-references/' + expected)
                data = self._fetch_original(meta, expected)
            super().put_blob(expected, data)
            return data

    put_receipt = put_blob
    get_receipt = get_blob

    def advance_paper_revision(
        self, round_id, stage, paper_id, expected_parent_hash, new_hash
    ):
        path = (
            "/internal/competition/scientific/papers/"
            + "/".join(quote(x, safe="") for x in (round_id, stage, paper_id))
            + "/revision"
        )
        try:
            result = self.backend._request(
                "POST", path,
                {"expected_parent_hash": expected_parent_hash, "new_hash": new_hash},
            )
        except RuntimeError as exc:
            if 'HTTP 409' in str(exc):
                raise Conflict('Remote scientific predecessor changed') from exc
            raise
        if result != {"sha256": new_hash}:
            raise ValueError("Remote changed scientific head")
        return new_hash

    def head(self,key):
        if key.startswith('paper:'):
            round_id, stage, paper_id = json.loads(key[len('paper:'):])
            path = '/internal/competition/scientific/papers/' + '/'.join(
                quote(x, safe='') for x in (round_id, stage, paper_id)) + '/revision'
            return self.backend._request('GET', path)['sha256']
        response=self.backend._request('GET','/internal/competition/scientific/heads/'+quote(key,safe=''))
        return response['sha256']

    def advance(self,key,parent,child):
        self.get_blob(child)
        try:
            response=self.backend._request('POST','/internal/competition/scientific/heads/'+quote(key,safe=''),
                {'expected_parent_hash':parent,'new_hash':child})
        except RuntimeError as exc:
            if 'HTTP 409' in str(exc):raise Conflict('Remote evidence predecessor changed') from exc
            raise
        if response!={'sha256':child}:raise ValueError('Remote changed logical evidence head')
        return child

    def generation(self,key,binding,*,activated_at=None,timeout=1800):
        import time
        head=self.head('generation-clock:'+key)
        if head is not None:
            value=json.loads(self.get_blob(head))
            if value['binding']!=binding:raise Conflict('Generation binding changed')
            return value['started'],value['deadline']
        start=time.time() if activated_at is None else activated_at
        value={'binding':binding,'started':start,'deadline':start+timeout}
        ref=self.put(value)
        try:self.advance('generation-clock:'+key,None,ref.sha256)
        except Conflict:
            return self.generation(key,binding,activated_at=activated_at,timeout=timeout)
        return start,start+timeout

    def reserve(self,operation,request,*,now=None,timeout=120,allow_new=True):
        import time
        from .store import Pending,Exhausted
        now=time.time() if now is None else now
        key='model-operation:'+operation;head=self.head(key)
        state=json.loads(self.get_blob(head)) if head else {'attempts':[]}
        attempts=state['attempts']
        for item in attempts:
            if item['response'] is not None and item['error'] is None:
                if request not in {item['request'],attempts[0]['request']}:raise Conflict('Successful operation request changed')
                return None,item['response']
        if not allow_new:
            raise Exhausted('Unchanged scientific generation deadline elapsed')
        if attempts and attempts[-1]['response'] is None and attempts[-1]['error'] is None:
            if attempts[-1]['expires']>now:raise Pending('Provider attempt is still live')
            attempts[-1]['error']='Dispatch outcome and billing unknown after retained timeout'
        if len(attempts)>=2:raise Exhausted('One technical retry exhausted')
        attempts.append({'ordinal':len(attempts)+1,'request':request,'expires':now+timeout,'response':None,'error':None})
        child=self.put(state)
        try:self.advance(key,head,child.sha256)
        except Conflict as exc:raise Pending('Another controller owns model dispatch') from exc
        return len(attempts),None

    def finish(self,operation,ordinal,*,response=None,error=None):
        key='model-operation:'+operation;head=self.head(key)
        if head is None:raise Conflict('Unknown model operation')
        state=json.loads(self.get_blob(head));item=state['attempts'][ordinal-1]
        if item['response'] is not None or item['error'] is not None:
            if (item['response'],item['error'])==(response,error):return
            raise Conflict('A terminal attempt cannot be overwritten by a late response')
        item.update(response=response,error=error)
        ref=self.put(state);self.advance(key,head,ref.sha256)

    def last_error(self,operation):
        head=self.head('model-operation:'+operation)
        if head is None:return None
        attempts=json.loads(self.get_blob(head))['attempts']
        return attempts[-1]['error'] if attempts else None
