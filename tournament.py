#!/usr/bin/env python3
"""Dependency-free, resumable, evidence-locked abstract tournament."""
import argparse
import fcntl
import concurrent.futures as futures
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time
import urllib.error
import urllib.request
from api_agents import roster, conversation
from api_budget import Budget, BudgetExceeded
from review_grounding import packet_for,bounded_schema,normalize_references,compact_draft_schema,reference_fact_schema,restore_evidence_references

ROOT = Path(__file__).resolve().parent
from prospective import (TASK_VERSION, LENSES, REVIEWERS, DRAFT_SCHEMA, NATURE_SCHEMA,
    COMMITTEE_SCHEMA, FACT_SCHEMA, REVIEW_SCHEMAS, draft_prompt, review_prompt,
    draft_checks as provenance_checks, rank, mock_draft, mock_review, mock_fact_review, task_contract)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('API redirect refused; credentials were not forwarded')

def read(path):
    return json.loads(Path(path).read_text())

def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj, indent=2)+'\n')
    temp.replace(path)

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()

def validate(value, schema, path='$'):
    """Validate the schema subset and report a precise field path for repair."""
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path+': expected one of '+repr(schema['enum']))
    kind=schema['type']
    if kind=='object':
        if not isinstance(value,dict):raise ValueError(path+': expected object')
        required=set(schema['required'])
        if set(value)!=required:
            raise ValueError(path+': missing fields '+str(sorted(required-set(value)))+'; unexpected fields '+str(sorted(set(value)-required)))
        for key,sub in schema['properties'].items():validate(value[key],sub,path+'.'+key)
    elif kind=='array':
        if not isinstance(value,list):raise ValueError(path+': expected array')
        for index,item in enumerate(value):validate(item,schema['items'],path+'['+str(index)+']')
    elif kind=='string':
        if not isinstance(value,str):raise ValueError(path+': expected string')
    elif kind=='integer':
        if type(value) is not int or not schema['minimum']<=value<=schema['maximum']:
            raise ValueError(path+': expected integer from '+str(schema['minimum'])+' to '+str(schema['maximum']))

def draft_checks(draft, evidence, word_limit):
    validate(draft, DRAFT_SCHEMA)
    return provenance_checks(draft, evidence, word_limit)

def incomplete_fact_review():
    return {'decision':'insufficient_primary_evidence', 'checks':[],
            'central_assumptions_checked':[], 'contradictions':[],
            'missing_primary_sources':['Fact-checker response exceeded the output limit; no complete review was accepted.'],
            'priority_revisions':['Obtain a complete source-grounded check of the central claims in the next round.'],
            'literature_assessment':[]}


def verify_fact_review(output, literature):
    """Fail closed on source errors without turning a bad review into a run crash."""
    output = json.loads(json.dumps(output))
    sources={x['id']:x for x in literature.get('primary_evidence',{}).get('sources',[])}
    issues=[]
    for index,check in enumerate(output['checks']):
        available=[sid for sid in check['source_ids'] if sid in sources]
        unknown=[sid for sid in check['source_ids'] if sid not in sources]
        quote=check['exact_evidence'].strip()
        mismatch=bool(check['source_ids'] and quote and not any(quote in sources[sid]['text'] for sid in available))
        if unknown or mismatch:
            issues.append({'check_index':index,'unknown_source_ids':unknown,'quote_mismatch':mismatch})
            check['verdict']='not_verified'
            check['correction']='Source verification failed; obtain an exact passage from an available primary source. '+check['correction']
    if issues:
        output['decision']='insufficient_primary_evidence'
        output['missing_primary_sources'].append('One or more source IDs or exact quotations failed automatic verification; see fact-validation audit.')
        output['priority_revisions'].append('Resolve source/quotation verification failures before treating these claims as established.')
    return output,issues

class ReviewFormatError(ValueError):
    """Repairable output, citation or truncation problem, not a scientific verdict."""


class Runner:
    def __init__(self, config, run_dir, mock=False):
        self.config, self.run_dir, self.mock = config, Path(run_dir), mock
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.evidence = read(self.run_dir/'evidence.lock.json')
        manifest_path = self.run_dir/'manifest.json'
        if not manifest_path.exists() or self.evidence != read(manifest_path)['evidence']:
            raise ValueError('Evidence lock does not match the immutable manifest')
        manifest=read(manifest_path)
        if manifest.get('task_contract') and manifest['task_contract'] != task_contract():
            raise ValueError('Task prompts or schemas changed; start a new run')
        self.actors=roster(config)
        if manifest.get('agent_roster') and manifest['agent_roster']!=self.actors:
            raise ValueError('Agent identities or model assignments changed; use a new run')
        self.budget=Budget(self.run_dir,config)
        self.literature = manifest.get('literature',{})
        self.model_literature={k:v for k,v in self.literature.items() if k not in ['query_logs','reviewer_reports','primary_evidence']}
        self.model_literature['reviewer_reports']=self.literature.get('reviewer_reports',{})
        lock = self.run_dir/'literature.lock.json'
        if self.literature and (not lock.exists() or read(lock) != self.literature):
            raise ValueError('Literature lock does not match immutable manifest')

    def task_progress(self,job_id,state,**metadata):
        return

    def make_conversation(self,job_id,prompt,schema):
        return conversation(job_id,prompt,schema,self.run_dir,self.config,self.actors)

    def call(self, job_id, prompt, schema):
        path = self.run_dir/'jobs'/f'{job_id}.json'
        audit=self.make_conversation(job_id,prompt,schema)
        fingerprint = digest({'prompt':prompt,'schema':schema,'config':self.config,'mock':self.mock,'agent_call':audit})
        if path.exists():
            cached = read(path)
            if cached['fingerprint'] != fingerprint: raise ValueError('Cached job input changed: '+job_id)
            validate(cached['output'],schema)
            return cached['output']
        save(self.run_dir/'prompts'/f'{job_id}.json',{'prompt':prompt,'schema':schema})
        save(self.run_dir/'agent_calls'/f'{job_id}.json',audit)
        if self.mock:
            output = self.mock_output(prompt, schema)
            usage = {}
        elif schema in (NATURE_SCHEMA, COMMITTEE_SCHEMA, FACT_SCHEMA):
            output,usage=self.review_with_repair(job_id,prompt,schema,audit)
        else:
            output, usage = self.api(prompt, schema, job_id)
        validate(output,schema)
        self.task_progress(job_id,'validated')
        save(path,{'fingerprint':fingerprint,'output':output,'usage':usage,'mock':self.mock})
        print('Saved: '+job_id, flush=True)
        return output

    def validate_review(self, output, schema):
        validate(output,schema)
        valid={pid for paper in self.literature.get('papers',[]) for pid in [paper['id'],*paper.get('aliases',[])]}
        citations={item['paper_id'] for item in output['literature_assessment']}
        if len(citations)<2 or not citations.issubset(valid):
            raise ReviewFormatError('literature_assessment: cite at least two distinct available paper IDs. Invalid IDs: '+str(sorted(citations-valid)))
        if schema==FACT_SCHEMA:
            _,issues=verify_fact_review(output,self.literature)
            if issues:
                raise ReviewFormatError('checks: exact_evidence must be a verbatim substring of a supplied primary source, and source_ids must use primary source IDs. Failures: '+json.dumps(issues))

    def review_with_repair(self, job_id, prompt, schema, audit):
        """One game-master correction turn, with durable inputs and normal billing."""
        attempts=[]
        active_job=job_id
        for turn in range(2):
            try:
                output,usage=self.api(prompt,schema,active_job)
                output,changes=normalize_references(output,packet_for(prompt,self.literature))
                save(self.run_dir/'reference_normalization'/f'{active_job}.json',{'changes':changes,'raw_response_preserved':True})
                self.validate_review(output,schema)
                save(self.run_dir/'review_repairs'/f'{job_id}.json',
                     {'status':'repaired' if turn else 'valid_first_submission','attempts':attempts,'accepted_job_id':active_job})
                return output,usage
            except ValueError as exc:
                attempts.append({'job_id':active_job,'error':str(exc)})
                self.task_progress(job_id,'repair_requested',validation_error=str(exc))
                save(self.run_dir/'review_repairs'/f'{job_id}.json',{'status':'validation_failed','attempts':attempts})
                if turn:break
                active_job=job_id.rsplit('-',1)[0]+'-repair1-'+job_id.rsplit('-',1)[1]
                response_path=self.run_dir/'responses'/f'{job_id}.json'
                previous=''
                if response_path.exists():
                    raw=read(response_path)
                    previous=''.join(c.get('text','') for item in raw.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text')
                correction=('GAME MASTER: Your submitted review failed output validation. You have one correction attempt. '
                    'Return a complete review in the required JSON schema. Repair only output, source-ID and quotation errors; '
                    'do not change a scientific judgment just to obtain a pass. A genuine evidence gap is acceptable. '
                    'Keep every field concise; use short verbatim evidence passages and no more than four decisive checks. '
                    'For baseline observations use claim_kind=baseline and an empty source_ids list; do not present baseline IDs as primary literature IDs. '
                    'Validation error: '+str(exc))
                repaired=json.loads(json.dumps(audit));repaired['job_id']=active_job
                if previous:repaired['messages'].append({'role':'assistant','content':previous})
                repaired['messages'].append({'role':'user','content':correction})
                repaired['repair_of']=job_id;repaired['validation_error']=str(exc)
                save(self.run_dir/'agent_calls'/f'{active_job}.json',repaired)
            except BudgetExceeded as exc:
                if 'Dedicated repair allowance' not in str(exc):raise
                attempts.append({'job_id':active_job,'error':str(exc)})
                break
        save(self.run_dir/'review_repairs'/f'{job_id}.json',{'status':'unverified_after_repair','attempts':attempts})
        if schema==FACT_SCHEMA:
            output=incomplete_fact_review()
            output['missing_primary_sources']=['No validated literature fact-check was obtained after the bounded repair step; see review_repairs audit.']
            output['priority_revisions']=['Resolve the recorded review validation errors; this review does not establish factual validity.']
            return output,{}
        raise RuntimeError('Review remains unavailable after bounded repair: '+job_id+'; see review_repairs audit. No scientific rejection was inferred.')

    def api(self, prompt, schema, job_id):
        if not self.config.get('allow_paid_run',False):
            raise RuntimeError('Paid execution is disabled: fixes only, no rerun authorized')
        # A saved response may exist even if interruption prevented job validation
        # or writing its cache. Recover it without issuing another paid request.
        response_path = self.run_dir/'responses'/f'{job_id}.json'
        if response_path.exists():
            data = read(response_path)
            if data.get('status') != 'completed':
                if schema in (NATURE_SCHEMA, COMMITTEE_SCHEMA, FACT_SCHEMA):
                    raise ReviewFormatError('API response truncated or incomplete: '+str(data.get('incomplete_details')))
                raise RuntimeError('Saved API response is incomplete for '+job_id+'; inspect before retrying')
            chunks = [c['text'] for item in data.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text']
            output = json.loads(''.join(chunks))
            contract_path=self.run_dir/'wire_contracts'/f'{job_id}.json'
            if contract_path.exists():output=restore_evidence_references(output,read(contract_path).get('evidence_options',{}))
            validate(output,schema)
            state = read(self.budget.path) if self.budget.path.exists() else {'entries':[]}
            unresolved = [entry for entry in state['entries'] if entry['job_id']==job_id and entry['status']=='pending_or_unknown']
            # With one unresolved attempt the saved response unambiguously settles
            # that reservation. Multiple unknown attempts stay conservatively held.
            if len(unresolved)==1:
                self.budget.settle(unresolved[0]['reservation_id'],data.get('usage'))
            print('Recovered saved response: '+job_id, flush=True)
            return output,data.get('usage',{})
        key = os.environ.get(self.config['api_key_env'])
        if not key: raise RuntimeError('API credential missing; configure '+self.config['api_key_env']+' outside the repository')
        audit_path=self.run_dir/'agent_calls'/f'{job_id}.json'
        audit=read(audit_path) if audit_path.exists() else conversation(job_id,prompt,schema,self.run_dir,self.config,self.actors)
        kind='writer' if schema==DRAFT_SCHEMA else ('nature' if schema==NATURE_SCHEMA else 'fact_checker' if schema==FACT_SCHEMA else 'committee')
        limit=audit.get('output_token_limit',self.config['output_token_limits'][kind])
        payload={'model':audit['model'],'input':audit['messages'],
            'text':{'format':{'type':'json_schema','name':'tournament_output','strict':True,'schema':schema}},
            'max_output_tokens':limit,'reasoning':{'effort':audit.get('reasoning_effort',self.config['reasoning_effort'])},
            'service_tier':'default','store':False}
        grounding=packet_for(prompt,self.literature)
        payload['text']['format']['schema']=bounded_schema(schema,grounding,fact=schema==FACT_SCHEMA)
        evidence_options={}
        if schema==FACT_SCHEMA:payload['text']['format']['schema'],evidence_options=reference_fact_schema(schema,grounding)
        if schema==DRAFT_SCHEMA and self.config.get('v6'):
            payload['text']['format']['schema']=compact_draft_schema(schema)
        if schema in (NATURE_SCHEMA,COMMITTEE_SCHEMA,FACT_SCHEMA):
            payload['input']=list(payload['input'])+[{'role':'user','content':'Output contract: bibliography paper IDs and primary passage IDs are different namespaces. Use only IDs allowed by the schema. For baseline observations leave source_ids empty. For exact_evidence select a literal offered quotation that belongs to the selected primary source; keep interpretation in scope/correction. At most four decisive checks and three literature references. Genuine evidence gaps and unfavorable judgments are valid.'}]
        style_path=ROOT/'operational_style.json'
        style=read(style_path) if style_path.exists() else None
        if style:payload['input'].append({'role':'user','content':style['instruction']})
        if evidence_options:
            payload['input'].append({'role':'user','content':json.dumps({'evidence_reference_contract':'Select evidence_ref from these options. Source IDs and exact quotations will be filled deterministically. Use NONE for baseline observations, hypothetical findings and unavailable evidence. A pointer verifies provenance, not support for a claim.','evidence_options':evidence_options},ensure_ascii=False,separators=(',',':'))})
        self.validate_payload(job_id,payload,audit)
        save(self.run_dir/'wire_contracts'/f'{job_id}.json',{'version':'grounded-review-v2','operational_style':style,'evidence_options':evidence_options,'schema':payload['text']['format']['schema'],'output_contract_added':schema in (NATURE_SCHEMA,COMMITTEE_SCHEMA,FACT_SCHEMA)})
        pricing=self.config.get('pricing_profiles',{}).get(payload['model'],self.config['pricing'])
        if payload['model'] != pricing['model']:
            raise ValueError('Model and pinned pricing differ; update the pricing profile before running')
        for attempt in range(self.config['max_attempts']):
            # Persist attempt reservations. Failed requests count toward the hard call cap, including on resume.
            import threading
            with API_LOCK:
                counter_path = self.run_dir/'api_attempts.json'
                attempts = read(counter_path) if counter_path.exists() else []
                if len(attempts) >= self.config['max_api_calls']: raise RuntimeError('Hard API call cap reached')
                # Recheck under the request lock: concurrent repairs must not
                # spend the same earned allowance.
                self.validate_payload(job_id,payload,audit)
                reservation=self.budget.reserve(job_id,payload,pricing)
                attempts.append({'job_id':job_id,'attempt':attempt+1,'time':time.time(),'reservation_id':reservation})
                save(counter_path,attempts)
            request = urllib.request.Request(self.config['base_url'].rstrip('/')+'/responses',
                data=json.dumps(payload).encode(), headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.config['timeout_seconds']) as response:
                    data = json.load(response)
                save(self.run_dir/'responses'/f'{job_id}.json',data)
                self.task_progress(job_id,'received',response_path=str(self.run_dir/'responses'/f'{job_id}.json'))
                self.budget.settle(reservation,data.get('usage'))
                if data.get('status') != 'completed':
                    if schema in (NATURE_SCHEMA, COMMITTEE_SCHEMA, FACT_SCHEMA):
                        raise ReviewFormatError('API response truncated or incomplete: '+str(data.get('incomplete_details')))
                    raise RuntimeError('API response did not complete; inspect token limit')
                chunks = [c['text'] for item in data.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text']
                output = restore_evidence_references(json.loads(''.join(chunks)),evidence_options)
                validate(output,schema)
                return output,data.get('usage',{})
            except urllib.error.HTTPError as exc:
                try:
                    error=json.loads(exc.read().decode()).get('error',{})
                    safe={k:str(error.get(k,'')) for k in ['code','type','param','message']}
                    safe={k:v.replace(key,'[REDACTED]') for k,v in safe.items()}
                    save(self.run_dir/'api_errors'/f'{job_id}.json',{'http_status':exc.code,'error':safe})
                except Exception:pass
                if exc.code not in [429,500,502,503,504] or attempt+1 == self.config['max_attempts']:
                    raise RuntimeError(f'API HTTP {exc.code} for {job_id}; response body suppressed to protect credentials') from None
                retry = exc.headers.get('Retry-After','')
                delay = float(retry) if retry.replace('.','',1).isdigit() else min(2**attempt,30)
                time.sleep(min(delay,60))
            except (urllib.error.URLError,TimeoutError):
                if attempt+1 == self.config['max_attempts']: raise RuntimeError('API connection failure for '+job_id) from None
                time.sleep(min(2**attempt,30))
        raise RuntimeError('API attempts exhausted')

    def validate_payload(self,job_id,payload,audit):
        """Versioned runners can enforce planned request envelopes before billing."""
        return

    def mock_output(self,prompt,schema):
        if schema == DRAFT_SCHEMA:
            return mock_draft(self.evidence)
        if schema==FACT_SCHEMA:return mock_fact_review()
        return mock_review(schema == NATURE_SCHEMA)

    def request_literature(self,reviewer=None):
        pack={k:v for k,v in self.literature.items() if k not in ['query_logs','reviewer_reports','primary_evidence']}
        reports=self.literature.get('reviewer_reports',{})
        pack['reviewer_reports']={reviewer:reports.get(reviewer,'')} if reviewer else {}
        if reviewer=='fact_checker':pack['primary_evidence']=self.literature.get('primary_evidence',{})
        return pack

    def batch(self, tasks):
        # An in-flight upper reservation can temporarily crowd out another job.
        # Wait for this batch to settle, then retry only jobs never sent for lack
        # of budget. A genuine exhausted cap still pauses; unknown calls stay held.
        results={};deferred=[];errors=[]
        with futures.ThreadPoolExecutor(max_workers=self.config['concurrency']) as pool:
            pending={pool.submit(self.call,*args):key for key,args in tasks.items()}
            for future in futures.as_completed(pending):
                key=pending[future]
                try:results[key]=future.result()
                except BudgetExceeded:deferred.append(key)
                except Exception as exc:errors.append(exc)
        if errors:raise errors[0]
        for key in deferred:results[key]=self.call(*tasks[key])
        return results

    def shortlist_saved_round(self,round_number):
        prefix=f'r{round_number}'
        mapping=read(self.run_dir/'private'/f'{prefix}-mapping.json')
        drafts={alias:read(self.run_dir/'jobs'/f'{alias}-draft.json')['output'] for alias in mapping}
        entries=[(alias,draft,[read(self.run_dir/'jobs'/f'{alias}-{role}.json')['output'] for role in REVIEWERS],
                  draft_checks(draft,self.evidence,self.config['word_limit'])) for alias,draft in drafts.items()]
        ranking=rank(entries,self.config['selection'],final=False)
        ranking_path=self.run_dir/f'{prefix}-ranking.json'
        if ranking_path.exists() and read(ranking_path)!=ranking:
            archive=self.run_dir/'history'/f'early-gate-correction-{time.time_ns()}'
            archive.mkdir(parents=True)
            for filename in [f'{prefix}-ranking.json',f'{prefix}-report.md','outcome.json']:
                old=self.run_dir/filename
                if old.exists():old.replace(archive/filename)
        save(ranking_path,ranking)
        shortlist=ranking[:self.config['round_sizes'][round_number]]
        save(self.run_dir/f'{prefix}-advancement.json',shortlist)
        save(self.run_dir/'continuation.json',{'policy':'relative shortlisting for revision; strict journal/feasibility gates only at final',
            'resumed_after_round':round_number,'reused_completed_api_calls':True,'same_budget_ledger':True,
            'shortlist':[x['alias'] for x in shortlist]})
        self.report(prefix,ranking,drafts)
        return {mapping[x['alias']]:(drafts[x['alias']],
                [read(self.run_dir/'jobs'/f"{x['alias']}-{role}.json")['output'] for role in REVIEWERS]) for x in shortlist}

    def run(self,start_round=1):
        previous = {}
        sizes = self.config['round_sizes']
        if start_round>1:previous=self.shortlist_saved_round(start_round-1)
        for round_index in range(start_round-1,len(sizes)-1):
            prefix = f'r{round_index+1}'
            mapping_path = self.run_dir/'private'/f'{prefix}-mapping.json'
            authors = list(range(sizes[0])) if round_index == 0 else list(previous)
            if mapping_path.exists(): mapping = read(mapping_path)
            else:
                random.Random(701+round_index).shuffle(authors)
                mapping = {f'{prefix}-candidate-{i+1:02d}':author for i,author in enumerate(authors)}
                save(mapping_path,mapping)
            if len(mapping) != len(authors): raise RuntimeError('Round population mismatch')
            tasks = {}
            for alias,author in mapping.items():
                prev, rev = previous.get(author,(None,None))
                prompt=draft_prompt(self.evidence,LENSES[author],self.config['word_limit'],prev,rev,self.request_literature())
                if prev is not None:
                    prompt['revision_brief']='Substantially redesign the scientific story and decisive experiments in response to the reviews. You may change hypothesis or direction; do not merely polish language. Advancement for revision is not Nature endorsement. Preserve the actual starting evidence and 24-month feasibility.'
                    prompt['validation_feedback']=draft_checks(prev,self.evidence,self.config['word_limit'])
                    prompt['planning_schema_notes']='baseline_ids may contain only E1, E2 or E3; put anticipated results under proposed experiment IDs. Dependencies mean finish-to-start: split enabling validation milestones into separate experiment IDs if later work can begin before all analysis/repeats finish.'
                tasks[alias] = (alias+'-draft',prompt,DRAFT_SCHEMA)
            drafts = self.batch(tasks)
            tasks = {f'{alias}/{reviewer}':(f'{alias}-{reviewer}',review_prompt(self.evidence,draft,reviewer,self.config['word_limit'],self.request_literature(reviewer)),REVIEW_SCHEMAS[reviewer])
                for alias,draft in drafts.items() for reviewer in REVIEWERS}
            reviews = self.batch(tasks)
            entries = [(alias,draft,[reviews[f'{alias}/{r}'] for r in REVIEWERS],draft_checks(draft,self.evidence,self.config['word_limit'])) for alias,draft in drafts.items()]
            final = round_index == len(sizes)-2
            ranking = rank(entries,self.config['selection'],final=final)
            save(self.run_dir/f'{prefix}-ranking.json',ranking)
            eligible = [r for r in ranking if r['eligible']]
            if not eligible:
                self.no_winner(prefix,ranking)
                self.report(prefix,ranking,drafts,blocked=True)
                print(f'{prefix}: no proposal passes both journal and feasibility gates; no winner',flush=True)
                return
            winners=eligible[:sizes[round_index+1]]
            save(self.run_dir/f'{prefix}-advancement.json',winners)
            previous = {mapping[r['alias']]:(drafts[r['alias']],[reviews[f"{r['alias']}/{j}"] for j in REVIEWERS]) for r in winners}
            self.report(prefix,ranking,drafts,final=final)
            print(f'{prefix}: reviewed {len(mapping)} proposals; advanced {len(winners)}',flush=True)
        winner = winners[0]['alias']
        save(self.run_dir/'winner.json',{'alias':winner,'draft':drafts[winner],'mock':self.mock,'artifact_status':'hypothetical_end_of_phd_paper','task_version':TASK_VERSION})

    def no_winner(self,prefix,ranking):
        # A corrected review must not leave an earlier winner or advancement
        # available to the next stage. Preserve superseded artifacts as history.
        round_number=int(prefix[1:])
        stale=[self.run_dir/'winner.json', self.run_dir/'FINAL.md']
        stale += [self.run_dir/f'r{i}-advancement.json' for i in range(round_number,len(self.config['round_sizes']))]
        existing=[path for path in stale if path.exists()]
        if existing:
            archive=self.run_dir/'history'/f'no-winner-{time.time_ns()}'
            archive.mkdir(parents=True)
            for path in existing:path.replace(archive/path.name)
        def best(key):
            candidates=[r for r in ranking if r['integrity_pass']]
            return max(candidates,key=lambda r:r[key])['alias'] if candidates else None
        save(self.run_dir/'outcome.json',{'status':'no_jointly_eligible_winner','round':prefix,
            'strongest_journal_concept':best('journal_score'),'strongest_committee_score':best('committee_score'),
            'note':'These separate distinctions are not jointly eligible winners. Review disagreements and revisions remain visible.'})

    def report(self,prefix,ranking,drafts,blocked=False,final=False):
        lines=['# '+('MOCK ONLY — ' if self.mock else '')+'Prospective two-year PhD paper tournament','',
            'All abstracts describe hypothetical finished papers. Anticipated findings are proposed outcomes, not obtained data.',
            'The Nature reviewer judges the finished paper; a separate fact-checker audits primary evidence; two committee reviewers assess quality and feasibility.',
            'Editorial merit and 24-month feasibility are separate gates; no winner is forced.','',
            'Stage: '+prefix+(' — NO JOINTLY ELIGIBLE PROPOSAL' if blocked else ''),'',
            '| Candidate | Advances / qualifies | Journal / 5 | Committee / 5 | Fact check | Journal decision | Committee decisions |',
            '|---|---|---|---|---|---|---|']
        for item in ranking:
            lines.append(f"| {item['alias']} | {item['eligible']} | {item['journal_score']} | {item['committee_score']} | {item['fact_check_decision']} | {', '.join(item['nature_decisions'])} | {', '.join(item['committee_decisions'])} |")
        for item in ranking:
            alias=item['alias'];draft=drafts[alias]
            lines += ['', '## '+alias,'',draft['title'],'',draft['abstract'],'',
                '**Core discovery:** '+draft['core_discovery'],
                '**Two-year plan:** see the complete structured draft in jobs/'+alias+'-draft.json.']
            for role in REVIEWERS:
                review=read(self.run_dir/'jobs'/f'{alias}-{role}.json')['output']
                lines += ['', '**'+role+' — '+review['decision']+'**',
                    'Scores: '+', '.join(f'{k}={v}' for k,v in review.get('scores',{}).items()),
                    review.get('publication_case',review.get('quality_assessment',''))]
                if 'feasibility_assessment' in review:lines.append(review['feasibility_assessment'])
                for key in ['story_gaps','baseline_contradictions','provenance_errors','unsupported_resource_assumptions','critical_path','required_scope_cuts','priority_revisions','contradictions','missing_primary_sources']:
                    if review.get(key):lines.append(key+': '+'; '.join(review[key]))
                lines += [f"Literature {x['paper_id']}: {x['overlap']} — {x['implication']}" for x in review['literature_assessment']]
        (self.run_dir/f'{prefix}-report.md').write_text('\n'.join(lines)+'\n')
        if final:
            top=next(r for r in ranking if r['eligible']);draft=drafts[top['alias']]
            save(self.run_dir/'outcome.json',{'status':'jointly_eligible_selection','round':prefix,
                'selected':top['alias'],'artifact_status':'hypothetical_end_of_phd_paper'})
            (self.run_dir/'FINAL.md').write_text('# Hypothetical end-of-PhD paper\n\n'+draft['title']+'\n\n'+draft['abstract']+'\n\nAnticipated findings, not obtained results. See structured plan and separate journal/committee reviews.\n')


import threading
API_LOCK = threading.Lock()

def plan(config):
    sizes = config['round_sizes']
    if len(sizes)!=4 or sizes[-1]!=1 or not 1<=sizes[0]<=len(LENSES) or any(a<=b for a,b in zip(sizes,sizes[1:])):
        raise ValueError('Use four decreasing round caps ending at one, with at most 20 initial writers')
    if not 1 <= config['concurrency'] <= 20: raise ValueError('Concurrency must be 1–20')
    if config['task_version'] != TASK_VERSION: raise ValueError('Wrong task version')
    return {'authors':sizes[0],'reviewers':4,'stages':sizes,'draft_calls':sum(sizes[:-1]),'review_calls':4*sum(sizes[:-1]),
        'planned_calls':5*sum(sizes[:-1]),'hard_call_cap':config['max_api_calls'],'concurrency':config['concurrency'],
        'model':config['model'],'provider':'mock' if config.get('mock') else config['base_url'],
        'task_version':TASK_VERSION,'selection':config['selection'],'budget_usd':config['budget_usd'],
        'api_model':config['model'],'execution':config['execution'],
        'note':'Maximum call counts; scheduled revisions precede final acceptance. Dollar reservations include in-flight calls, retries and unknown responses; no cache discounts assumed.'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','prepare','run','mock'])
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--evidence',type=Path,default=ROOT/'inputs/evidence.json')
    parser.add_argument('--literature',type=Path,default=ROOT/'literature/pack-fact-checker-v4.json')
    parser.add_argument('--run-dir',type=Path,default=ROOT/'runs/fact-checker-v4')
    parser.add_argument('--model',help='Explicit API model ID; no model is silently assumed')
    parser.add_argument('--start-round',type=int,choices=[1,2,3],default=1,help='Continue from saved completed reviews; never regenerate earlier API calls')
    args=parser.parse_args()
    config=read(args.config)
    if args.model: config['model']=args.model
    summary=plan(config)
    if args.command=='plan': print(json.dumps(summary,indent=2)); return
    if args.command=='run':
        if not config.get('allow_paid_run',False):parser.error('Paid execution is disabled: user authorized fixes only, no rerun')
        if not config['model']: parser.error('Set --model or config.model before a live run')
        if not os.environ.get(config['api_key_env']): parser.error('Missing credential: '+config['api_key_env'])
        if config['max_api_calls'] < summary['planned_calls']: parser.error('Call cap is below planned calls')
    mock=args.command=='mock'
    literature=read(args.literature) if args.literature.exists() else {}
    if args.command=='run' and (not literature.get('papers') or set(literature.get('reviewer_reports',{})) != set(REVIEWERS)):
        parser.error('Live run requires a literature pack with papers and all four reviewer research reports')
    manifest={'config':config,'evidence':read(args.evidence),'literature':literature,'mock':mock,'task_contract':task_contract(),'agent_roster':roster(config)}
    run_dir=args.run_dir
    run_dir.mkdir(parents=True,exist_ok=True)
    process_lock = (run_dir/'.run.lock').open('a')
    try:
        fcntl.flock(process_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another process is using this run directory')
    if (run_dir/'manifest.json').exists():
        if read(run_dir/'manifest.json') != manifest: parser.error('Inputs changed; use a new run directory')
    else:
        save(run_dir/'manifest.json',manifest)
        save(run_dir/'evidence.lock.json',manifest['evidence'])
        save(run_dir/'literature.lock.json',literature)
    if not (run_dir/'evidence.lock.json').exists() or read(run_dir/'evidence.lock.json') != manifest['evidence']:
        parser.error('Evidence lock missing or modified; use a new run directory')
    if not (run_dir/'literature.lock.json').exists() or read(run_dir/'literature.lock.json') != literature:
        parser.error('Literature lock missing or modified; use a new run directory')
    evidence=manifest['evidence']
    save(run_dir/'agents.json',roster(config))
    for index,lens in enumerate(LENSES[:config['round_sizes'][0]]):
        save(run_dir/'agent_packets'/f'author-{index+1:02d}.json',{'schema':DRAFT_SCHEMA,'prompt':draft_prompt(evidence,lens,config['word_limit'],literature=literature)})
    for reviewer,role in REVIEWERS.items():
        save(run_dir/'agent_packets'/f'{reviewer}.json',{'schema':REVIEW_SCHEMAS[reviewer],'prompt_template':review_prompt(evidence,{},reviewer,config['word_limit'],literature),'instructions':'Instantiate via review_prompt. Nature must never receive the baseline or plan.'})
    save(run_dir/'plan.json',summary)
    if args.command=='prepare': print(f"Prepared {config['round_sizes'][0]} API writer personas, four reviewer personas and locked inputs. No API calls made."); return
    Runner(config,run_dir,mock).run(start_round=args.start_round)

if __name__=='__main__': main()
