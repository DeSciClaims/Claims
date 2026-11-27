"""Parallel verification must keep every paper and propagate every failure."""
import importlib.util,threading,time,copy
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('seed_reader',Path(__file__).parents[1]/'scripts/scientific_seed_continuation_reader.py')
reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)


def test_bounded_batches_preserve_all_existing_verification(tmp_path):
    rows=[{'paper_id':f'p{i:02}','challenger':str(j)} for i in range(33) for j in range(3)]
    before=copy.deepcopy(rows);lock=threading.Lock();active=0;maximum=0;seen=[]
    def original(rid,stage,*,backend,pairs,round_record):
        nonlocal active,maximum
        assert rid=='round' and stage=='final' and round_record=={'frozen':True}
        with lock:active+=1;maximum=max(maximum,active);seen.extend(pairs)
        time.sleep(.01)
        with lock:active-=1
        return [{'paper_id':pid,'original_verification':'passed'} for pid in sorted({p['paper_id'] for p in pairs})]
    result=reader.verify_batches(original,'round','final',backend=None,pairs=rows,round_record={'frozen':True})
    assert [r['paper_id'] for r in result]==[f'p{i:02}' for i in range(33)]
    assert 1<maximum<=10 and len(seen)==len(rows) and rows==before
    assert sorted((r['paper_id'],r['challenger']) for r in seen)==sorted((r['paper_id'],r['challenger']) for r in rows)


def test_any_failed_paper_still_blocks_verification():
    def original(rid,stage,*,backend,pairs,round_record):
        if any(p['paper_id']=='p08' for p in pairs):raise ValueError('invalid retained receipt')
        return [{'paper_id':p['paper_id']} for p in pairs]
    with pytest.raises(ValueError,match='invalid retained receipt'):
        reader.verify_batches(original,'round','final',backend=None,pairs=[{'paper_id':f'p{i:02}'} for i in range(17)],round_record={})
