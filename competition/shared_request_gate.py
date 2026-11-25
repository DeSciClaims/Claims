"""Crash-safe host-wide request slots shared by source and scoring processes."""
from pathlib import Path
import fcntl
import json
import os
import threading
import time


class SharedRequestGate:
    def __init__(self, directory, limit):
        if type(limit) is not int or not 1<=limit<=500:
            raise ValueError('Shared request limit must be between 1 and 500')
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.limit=limit;self.local=threading.BoundedSemaphore(limit)
        self.held=threading.local();self.lock=threading.Lock();self.next_slot=os.getpid()%limit
        with (self.directory/'configuration.lock').open('a+') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            path=self.directory/'limit.json'
            if path.exists():
                if json.loads(path.read_text())!={'limit':limit}:
                    raise ValueError('Live shared request gate has a different frozen limit')
            else:path.write_text(json.dumps({'limit':limit}))

    def __enter__(self):
        if getattr(self.held,'descriptor',None) is not None:
            raise ValueError('Shared request admission cannot be nested')
        self.local.acquire()
        try:
            while True:
                with self.lock:
                    start=self.next_slot;self.next_slot=(start+1)%self.limit
                for offset in range(self.limit):
                    slot=(start+offset)%self.limit
                    descriptor=os.open(self.directory/f'{slot}.slot',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
                    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:os.close(descriptor)
                    except BaseException:os.close(descriptor);raise
                    else:
                        self.held.descriptor=descriptor
                        return self
                time.sleep(.05)
        except BaseException:
            self.local.release();raise

    def __exit__(self,*exc):
        descriptor=self.held.descriptor;self.held.descriptor=None
        try:os.close(descriptor)  # flock also releases when the process exits.
        finally:self.local.release()
