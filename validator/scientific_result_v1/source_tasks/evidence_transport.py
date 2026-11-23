"""Bounded retries for immutable evidence and idempotent scientific checkpoints."""
import time
from urllib.error import HTTPError,URLError


def retry_evidence(call):
    for attempt in range(3):
        try:return call()
        except (HTTPError,URLError,OSError,RuntimeError) as exc:
            status=getattr(exc,'code',None)
            retryable=(status in {408,429,500,502,503,504} if status is not None else
                isinstance(exc,(URLError,OSError)) or str(exc)=='competition backend is unavailable' or
                str(exc) in {'competition backend returned HTTP '+str(n) for n in (408,429,500,502,503,504)})
            if not retryable or attempt==2:raise
            time.sleep(2**attempt)


def evidence_request(backend,method,path,payload=None):
    if not path.startswith('/internal/competition/scientific/'):
        raise ValueError('Evidence retry cannot issue a general competition operation')
    if method not in {'GET','POST'} or method=='POST' and not (
        '/heads/' in path or path.endswith('/revision') or '/recoveries/' in path and '/papers/' in path):
        raise ValueError('Evidence write is not an idempotent checkpoint')
    return retry_evidence(lambda:backend._request(method,path,payload) if payload is not None else backend._request(method,path))
