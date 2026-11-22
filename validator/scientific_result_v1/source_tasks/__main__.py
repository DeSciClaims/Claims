"""Replay a retained scientific receipt without initializing a provider or model."""
import argparse
import json
from .store import Store
from .contracts import FileRef
from .replay import replay


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['replay'])
    parser.add_argument('--store',required=True)
    parser.add_argument('--receipt-sha256',required=True)
    args=parser.parse_args();store=Store(args.store)
    raw=store.get_blob(args.receipt_sha256)
    ref=FileRef(relative_path='blobs/'+args.receipt_sha256,sha256=args.receipt_sha256,byte_length=len(raw),media_type='application/octet-stream')
    result=replay(store,ref)
    print(json.dumps({'state':result.state,'receipt_sha256':ref.sha256,'scores':[
        {'output_alias':v.output_alias,'state':v.state,'Q':v.score.model_dump() if v.score else None} for v in result.paper_scores]}))

if __name__=='__main__':main()
