"""Separate, fixed P-census sampling diagnostic from guide §7.4."""
from fractions import Fraction
import hashlib,hmac,math
from .sampling import frame,ordered
from .scoring import quality,mean


def qualification_commitment(secret,pilot_manifest_sha256):
    if len(secret)!=32:raise ValueError('Qualification secret must be 32 bytes')
    return hashlib.sha256(frame('QUALIFICATION_P_CENSUS',pilot_manifest_sha256)+secret).hexdigest()


def census_papers(ids,secret,pilot_manifest_sha256):
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate pilot paper')
    rank=lambda key:hmac.new(secret,frame('QUALIFICATION_P_CENSUS',pilot_manifest_sha256,key),hashlib.sha256).digest()
    return sorted(ids,key=lambda key:(rank(key),key.encode()))[:10]


def _fraction(value):
    if value is None:return None
    if isinstance(value,Fraction):return value
    if type(value) is int:return Fraction(value)
    return Fraction(value['numerator'],value['denominator'])


def _pq(output,keys):
    if output['gate']==0:return None,Fraction(0)
    bits=output['bucket_bits']
    if output.get('unanchored_bit')==0:p=Fraction(0)
    else:p=mean(Fraction(bits[k]) for k in keys) if keys else Fraction(1)
    q=quality(gate=1,content=_fraction(output['content']),archive=_fraction(output['archive']),audit=p,
        content_available=output['content_available'],archive_available=output['archive_available'])
    return p,q


def diagnostic(papers,*,pipeline_ids,secret,pilot_manifest_sha256):
    if len(pipeline_ids)!=2 or pipeline_ids[0]==pipeline_ids[1]:raise ValueError('Diagnostic needs two distinct scored pipelines')
    selected=census_papers([p['paper_id'] for p in papers if p['eligible']],secret,pilot_manifest_sha256)
    byid={p['paper_id']:p for p in papers};complete=[];incomplete=[]
    for key in selected:
        paper=byid[key]
        if set(paper['outputs'])!=set(pipeline_ids):raise ValueError('Diagnostic pipeline grid differs')
        good=True
        for output in paper['outputs'].values():
            if output['gate']==0:continue
            if type(output['gate']) is not int or output['gate']!=1:good=False
            if output['content_available'] and output['content'] is None:good=False
            if output['archive_available'] and output['archive'] is None:good=False
            if output.get('unanchored_required') and output.get('unanchored_bit') is None:good=False
            if any(type(v) is not int or v not in {0,1} for v in output['bucket_bits'].values()):good=False
        (complete if good else incomplete).append(key)
    def encoded(value):return None if value is None else {'numerator':value.numerator,'denominator':value.denominator}
    census={key:{p:_pq(byid[key]['outputs'][p],list(byid[key]['outputs'][p]['bucket_bits'])) for p in pipeline_ids} for key in complete}
    base_q={p:mean(census[k][p][1] for k in complete) if complete else None for p in pipeline_ids}
    base_p={p:mean(census[k][p][0] for k in complete if census[k][p][0] is not None) if any(census[k][p][0] is not None for k in complete) else None for p in pipeline_ids}
    base_gain=base_q[pipeline_ids[1]]-base_q[pipeline_ids[0]] if complete else None
    def errors(values,truth):
        if truth is None:return {'reason':'INCOMPLETE_CENSUS','mae':None,'rmse':None,'p95_absolute_error':None}
        differences=[abs(v-truth) for v in values]
        return {'mae':float(mean(differences)),'rmse':math.sqrt(float(mean(d*d for d in differences))),
            'p95_absolute_error':float(sorted(differences)[math.ceil(.95*len(differences))-1])}
    results={}
    for size in (2,4,6,10):
        q_values={p:[] for p in pipeline_ids};p_values={p:[] for p in pipeline_ids};gains=[];replicates=[]
        for i in range(1,2001):
            derived=hmac.new(secret,frame('QUALIFICATION_P_RESAMPLE',str(i)),hashlib.sha256).digest()
            measured={}
            for key in complete:
                paper=byid[key];scope=tuple(paper['scope']);measured[key]={}
                for p in pipeline_ids:
                    output=paper['outputs'][p];keys=ordered(output['bucket_bits'],derived,scope,'P_BUCKET')[:size]
                    measured[key][p]=_pq(output,keys)
            qs={p:mean(measured[k][p][1] for k in complete) if complete else None for p in pipeline_ids}
            ps={p:mean(measured[k][p][0] for k in complete if measured[k][p][0] is not None) if base_p[p] is not None else None for p in pipeline_ids}
            gain=qs[pipeline_ids[1]]-qs[pipeline_ids[0]] if complete else None
            replicates.append({'replicate':i,'Q':{p:encoded(qs[p]) for p in pipeline_ids},'P':{p:encoded(ps[p]) for p in pipeline_ids},'paired_gain':encoded(gain)})
            for p in pipeline_ids:
                if qs[p] is not None:q_values[p].append(qs[p])
                if ps[p] is not None:p_values[p].append(ps[p])
            if gain is not None:gains.append(gain)
        results[str(size)]={'replicates':replicates,'Q_errors':{p:errors(q_values[p],base_q[p]) for p in pipeline_ids},
            'P_errors':{p:errors(p_values[p],base_p[p]) for p in pipeline_ids},'gain_errors':errors(gains,base_gain),
            'order_reversal_rate':sum(g*base_gain<0 for g in gains)/len(gains) if base_gain else None,
            'sampled_tie_count':sum(g==0 for g in gains),'census_gap_at_least_0_02':abs(base_gain)>=Fraction(1,50) if base_gain is not None else None}
    return {'pilot_manifest_sha256':pilot_manifest_sha256,'qualification_commitment':qualification_commitment(secret,pilot_manifest_sha256),
        'revealed_qualification_secret':secret.hex(),'selected_paper_ids':selected,'complete_paired_papers':len(complete),'incomplete_paired_papers':len(incomplete),
        'resamples':2000,'census_Q':{p:encoded(base_q[p]) for p in pipeline_ids},'census_P':{p:encoded(base_p[p]) for p in pipeline_ids},
        'census_paired_gain':encoded(base_gain),'results':results}
