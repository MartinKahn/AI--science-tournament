"""Constrain citation namespaces and quotation output before generation.

Exact quotation matching verifies provenance, never substantive claim support.
"""
import copy
import json
import re


def packet_for(prompt,fallback):
    literature=prompt.get('literature',fallback)
    return {'papers':literature.get('papers',fallback.get('papers',[])),
            'primary_evidence':prompt.get('primary_evidence',literature.get('primary_evidence',fallback.get('primary_evidence',{})))}


def quote_choices(sources):
    choices=['']
    for source in sources:
        text=source['text']
        spans=[s.strip() for line in re.split(r'[\r\n\t]',text) for s in re.split(r'(?<=[.!?])\s+',line) if s.strip()]
        def score(s):return sum(s.lower().count(k) for k in ['control','replication','benchmark','measurement','uncertainty'])
        for span in sorted(spans,key=score,reverse=True)[:2]:
            quote=span[:300].rstrip()
            if quote and quote not in choices:choices.append(quote)
    return choices


def bounded_schema(schema,packet,fact=False):
    result=json.loads(json.dumps(schema))
    if 'literature_assessment' not in result.get('properties',{}):return result
    papers=sorted({pid for p in packet.get('papers',[]) for pid in [p['id'],*p.get('aliases',[])]})
    if papers:result['properties']['literature_assessment']['items']['properties']['paper_id']['enum']=papers
    result['properties']['literature_assessment']['maxItems']=3
    if fact:
        sources=packet.get('primary_evidence',{}).get('sources',[])
        checks=result['properties']['checks'];checks['maxItems']=4
        check=checks['items']['properties']
        ids=sorted(s['id'] for s in sources)
        if ids:check['source_ids']['items']['enum']=ids
        check['source_ids']['maxItems']=1
        check['exact_evidence']['enum']=quote_choices(sources)
        for key,limit in [('claim',300),('location',180),('scope',350),('correction',450)]:check[key]['maxLength']=limit
        for key in ['central_assumptions_checked','contradictions','missing_primary_sources','priority_revisions']:
            result['properties'][key]['maxItems']=4
            result['properties'][key]['items']['maxLength']=400
    return result


def compact_draft_schema(schema):
    """Bound item counts, without hard character limits that can stall decoding."""
    result=json.loads(json.dumps(schema));props=result['properties']
    for key,count in [('baseline_use',3),('claim_map',6),('proposed_experiments',4),('milestones',8),('scope_cuts',3)]:props[key]['maxItems']=count
    baseline=['E1','E2','E3'];experiments=['P1','P2','P3','P4']
    props['baseline_use']['items']['properties']['evidence_id']['enum']=baseline
    props['claim_map']['items']['properties']['baseline_ids']['items']['enum']=baseline
    props['claim_map']['items']['properties']['experiment_ids']['items']['enum']=experiments
    props['proposed_experiments']['items']['properties']['id']['enum']=experiments
    props['proposed_experiments']['items']['properties']['dependencies']['items']['enum']=experiments
    return result


def normalize_references(output,packet):
    """Only resolve unique explicit IDs; never rewrite claims or invented quotes."""
    result=copy.deepcopy(output);changes=[]
    papers=packet.get('papers',[]);known={pid for p in papers for pid in [p['id'],*p.get('aliases',[])]}
    for item in result.get('literature_assessment',[]):
        old=item['paper_id']
        if old in known:continue
        match=re.fullmatch(r'(?:PUBMED-|PMID:|PMID-)(\d+)',old)
        candidates={p['id'] for p in papers if match and str(p.get('pmid',''))==match.group(1)}
        if len(candidates)==1:
            item['paper_id']=next(iter(candidates));changes.append({'field':'paper_id','from':old,'to':item['paper_id']})
    sources=packet.get('primary_evidence',{}).get('sources',[]);source_ids={s['id'] for s in sources}
    for index,check in enumerate(result.get('checks',[])):
        old=check['source_ids']
        if check['claim_kind']=='baseline' and old and all(s in ['E1','E2','E3'] for s in old):
            check['source_ids']=[];changes.append({'check':index,'from':old,'to':[],'reason':'baseline experiment IDs are not literature IDs'});continue
        if old and any(s not in source_ids for s in old):
            quote=check['exact_evidence'].strip()
            matches=[s['id'] for s in sources if quote and quote in s['text'] and s['paper_id'] in old]
            if len(matches)==1:
                check['source_ids']=matches;changes.append({'check':index,'from':old,'to':matches,'reason':'unique exact passage within explicitly cited paper'})
    return result,changes

def reference_fact_schema(schema,packet):
    """Select one evidence pointer instead of independently generating source+quote."""
    result=bounded_schema(schema,packet,True)
    options={}
    for source in packet.get('primary_evidence',{}).get('sources',[]):
        for quote in quote_choices([source])[1:]:
            label='Q'+str(len(options)+1).zfill(3)
            options[label]={'source_id':source['id'],'text':quote}
    check=result['properties']['checks']['items']
    check['properties'].pop('source_ids');check['properties'].pop('exact_evidence')
    check['properties']['evidence_ref']={'type':'string','enum':['NONE',*options]}
    check['required']=list(check['properties'])
    return result,options


def restore_evidence_references(output,options):
    result=copy.deepcopy(output)
    for check in result.get('checks',[]):
        if 'evidence_ref' not in check:continue  # Historical logical-format response.
        reference=check.pop('evidence_ref')
        if reference=='NONE':check.update(source_ids=[],exact_evidence='')
        elif reference in options:
            option=options[reference];check.update(source_ids=[option['source_id']],exact_evidence=option['text'])
        else:raise ValueError('Unavailable evidence reference: '+reference)
    return result
