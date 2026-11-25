import multiprocessing
import time
from queue import Empty
import pytest
from competition.shared_request_gate import SharedRequestGate


def enter(directory,limit,ready,release):
    with SharedRequestGate(directory,limit):
        ready.put('entered');release.wait(8)


def hold_for_crash(directory,pipe):
    with SharedRequestGate(directory,1):
        pipe.send('entered');time.sleep(30)


def test_gate_limits_independent_processes(tmp_path):
    ctx=multiprocessing.get_context('spawn');ready=ctx.Queue();release=ctx.Event()
    children=[ctx.Process(target=enter,args=(str(tmp_path),2,ready,release)) for _ in range(3)]
    try:
        for child in children:child.start()
        assert ready.get(timeout=8)=='entered' and ready.get(timeout=8)=='entered'
        with pytest.raises(Empty):ready.get(timeout=.25)
        release.set();assert ready.get(timeout=8)=='entered'
        for child in children:child.join(8);assert child.exitcode==0
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(3)


def test_crashed_process_releases_slot(tmp_path):
    ctx=multiprocessing.get_context('spawn');read,write=ctx.Pipe(duplex=False)
    first=ctx.Process(target=hold_for_crash,args=(str(tmp_path),write));first.start()
    assert read.poll(8) and read.recv()=='entered';first.terminate();first.join(5)
    read.close();write.close()
    ready=ctx.Queue();release=ctx.Event()
    second=ctx.Process(target=enter,args=(str(tmp_path),1,ready,release));second.start()
    try:
        assert ready.get(timeout=8)=='entered';release.set();second.join(5);assert second.exitcode==0
    finally:
        if second.is_alive():second.terminate();second.join(3)


def test_gate_rejects_conflicting_limit(tmp_path):
    SharedRequestGate(tmp_path,2)
    with pytest.raises(ValueError,match='different frozen limit'):SharedRequestGate(tmp_path,3)
