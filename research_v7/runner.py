"""Research-first, source-aware tournament with private competitive feedback."""
import concurrent.futures as futures
import copy
import hashlib
import json
import random
import re
import threading
from pathlib import Path
from prospective import (DRAFT_SCHEMA,NATURE_SCHEMA,FACT_SCHEMA,REVIEWERS,LENSES,rank,
    mock_draft,mock_review,mock_fact_review)
from tournament import read,save,draft_checks,ReviewFormatError
from review_grounding import normalize_references
from research_v6.runner import ResearchRunner
from research_v6.scheduler import PausedTask,CoordinatorRequired
from research_v6.design import CARD_SCHEMA,GROUP_SCHEMA,META_SCHEMA,select_diverse,validate_groups,safe_meta,meta_review_brief
from research_v7.contracts import *
from research_v7.budget import workflow_spec
from research_v7.decisions import standings,verify_improvement,verdict,classify_issues

class TournamentV7(ResearchRunner):
    def __init__(self,config,run_dir,mock=False):
        super().__init__(config,run_dir,mock)
        self.contract=read(self.run_dir/'manifest.json')['v7_contract']
        self.source_lock=threading.RLock()
        self.seed_sources={x['id'] for x in self.literature['primary_evidence']['sources']}
        self.author_source_ids={i:set() for i in range(4)}
        self.packets={}
    def prompt(self,name):return self.contract['prompts'][name]
    def coordinator_output(self,job,schema,context,checker=None):
        path=self.run_dir/'coordinator_fixes'/(job+'.json')
        if path.exists():
            record=read(path)
            if not record.get('reason'):raise ValueError('Coordinator fix needs an audit reason')
            output=record['output'];validate_extra(output,schema)
            if checker:output=checker(output)
            save(self.run_dir/'coordinator_validations'/(job+'.json'),{
                'origin':'main_agent','reason':record['reason'],'validated':True})
            return output
        save(self.run_dir/'coordinator_handoffs'/(job+'.json'),{
            'job':job,'status':'needs_main_agent_repair','schema':schema,'context':context,
            'fix_path':str(path),'instructions':'Main agent: supply output plus reason at fix_path. Preserve scientific judgments and baseline facts. Repair structure/provenance only; no invented data, sources or favorable grades. The same validators run again. No candidate may be eliminated for this failure.'})
        raise CoordinatorRequired('Main-agent repair required: '+job+'; candidate retained, no scientific rejection')
    def validate_payload(self,job_id,payload,audit):
        # Input envelopes are planning estimates, not execution limits in V7.
        # The billing layer reserves the actual serialized request before sending.
        # Retain the inherited repair-budget check without rejecting larger context.
        from api_budget import input_token_bound
        size=input_token_bound(payload)-4096
        adjusted=dict(audit,input_byte_limit=max(audit['input_byte_limit'],size))
        save(self.run_dir/'input_sizes'/(job_id+'.json'),{
            'actual_input_bytes':size,'planned_input_bytes':audit['input_byte_limit'],
            'policy':'No fixed byte rejection; actual-payload USD reservation enforced'})
        return super().validate_payload(job_id,payload,adjusted)
    def parallel(self,items,action):
        result={}
        with futures.ThreadPoolExecutor(max_workers=self.config['concurrency']) as pool:
            active={pool.submit(action,key,value):key for key,value in items.items()}
            errors=[]
            for future in futures.as_completed(active):
                try:result[active[future]]=future.result()
                except Exception as exc:errors.append(exc)
        if errors:raise errors[0]
        return result
    def invoke(self,job,role,stage,prompt,schema,actor=None,reviewer=None,dependencies=(),checker=None):
        actor_id='writer-'+str(actor+1).zfill(2) if actor is not None else None
        spec=workflow_spec(self.config,role,stage,actor=actor_id,reviewer=reviewer)
        system=(self.actors[actor_id]['system_prompt'] if actor_id and role in ['brief','brief_revision','composition','edit'] else
                self.actors[reviewer]['system_prompt'] if reviewer else 'You are a research coordinator. Use only the supplied data. Return concise JSON in the requested schema.')
        audit={'actor_id':actor_id or reviewer or role,'job_id':job,'model':spec['model'],
          'reasoning_effort':spec['reasoning_effort'],'output_token_limit':spec['max_output_tokens'],
          'input_byte_limit':spec['max_input_bytes'],'budget_role':role,
          'messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(prompt,ensure_ascii=False,separators=(',',':'))}],
          'history_policy':'Only this writer/candidate and its authorized private feedback. No competing submissions.'}
        if role in ['brief','brief_revision']:
            schema=json.loads(json.dumps(schema))
            schema['properties']['closest_studies']['items']['properties']['source_id']['enum']=[s['id'] for s in prompt['literature']['primary_evidence']['sources']]
        inputs={'prompt':prompt,'schema':schema,'model_policy':spec,'actor':actor_id,'reviewer':reviewer}
        def action():
            if (self.run_dir/'coordinator_fixes'/(job+'.json')).exists():
                output=self.coordinator_output(job,schema,{'prompt':prompt},checker)
                save(self.run_dir/'jobs'/(job+'.json'),{'output':output,'accepted_job_id':None,
                    'accepted_origin':'main_agent','mock':self.mock,'role':role})
                return output
            save(self.run_dir/'agent_calls'/(job+'.json'),audit)
            active=job
            failures=[]
            for attempt in range(4):
                try:
                    if self.mock:output=self.mock_output_v7(role,prompt,actor)
                    else:output,_=self.api(prompt,schema,active)
                    validate_extra(output,schema)
                    if checker:output=checker(output)
                    save(self.run_dir/'jobs'/(job+'.json'),{'output':output,'accepted_job_id':active,'mock':self.mock,'role':role})
                    return output
                except (ValueError,RuntimeError) as exc:
                    if isinstance(exc,PausedTask) or 'Budget' in type(exc).__name__ or not (isinstance(exc,ValueError) or 'complete' in str(exc)):raise
                    failures.append({'attempt':attempt,'response':active,'error':str(exc)})
                    if self.mock:raise
                    if attempt==3:
                        return self.coordinator_output(job,schema,{'prompt':prompt,'failures':failures,
                            'audit':audit,'response_directory':str(self.run_dir/'responses')},checker)
                    previous=active
                    active=job+'-repair1-format'+('' if attempt==0 else '-'+str(attempt+1))
                    repair=copy.deepcopy(audit);repair['job_id']=active;repair['repair_of']=job
                    if role=='brief':
                        completion=workflow_spec(self.config,'brief_revision',stage)
                        for key in ['model','reasoning_effort']:repair[key]=completion[key]
                        repair['output_token_limit']=completion['max_output_tokens']
                        repair['seed_model']=audit['model']
                    original=self.run_dir/'responses'/(previous+'.json')
                    if original.exists():
                        data=read(original);text=''.join(c.get('text','') for item in data.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text').rstrip()
                        if text:repair['messages'].append({'role':'assistant','content':text[:16000]})
                    repair['messages'].append({'role':'user','content':'GAME MASTER: return this response to its author for format/citation repair '+str(attempt+1)+'. Preserve scientific judgment, unfavorable grades and actual baseline facts. Repair exact quotations against the supplied text; do not demand completed results for planned experiments. Regenerate complete concise JSON. Error: '+str(exc)})
                    save(self.run_dir/'agent_calls'/(active+'.json'),repair)
            raise RuntimeError('Unreachable repair state')
        recoverable=(self.run_dir/'responses'/(job+'.json')).exists() or any((self.run_dir/'responses').glob(job+'-repair1-format*.json')) or (self.run_dir/'coordinator_fixes'/(job+'.json')).exists()
        return self.scheduler.execute(job,inputs,action,dependencies=dependencies,recoverable=recoverable,retry_unknown=self.retry_unknown)
    def mock_output_v7(self,role,prompt,actor):
        if role=='search':return {'queries':[]}
        if role in ['brief','brief_revision']:
            sources=prompt['literature']['primary_evidence']['sources'];picked=[];seen=set()
            for source in sources:
                if source['paper_id'] not in seen:picked.append(source);seen.add(source['paper_id'])
                if len(picked)==3:break
            return {'question':'MOCK ONLY distinct question '+str(actor),'known_findings':'Mock source-linked baseline',
              'closest_studies':[{'source_id':s['id'],'established_result':'Mock','difference_from_proposal':'Mock'} for s in picked],
              'proposed_advance':'MOCK ONLY advance '+str(actor),'decisive_experiment':'MOCK ONLY discriminator','competing_explanation':'MOCK ONLY alternative',
              'resources':[],'open_questions':[],'novelty_status':'provisional','search_scope':'Offline fixture, not scientific validation'}
        if role=='brief_review':return {'decision':'ready','issues':[],'queries':[],'lab_questions':[],'required_changes':[]}
        if role=='composition':
            d=mock_draft(self.evidence);d['title']='MOCK ONLY proposal '+str(actor);d['core_discovery']='MOCK distinct advance '+str(actor);return d
        if role=='technical':return {'decision':'pass','issues':[],'summary':'Mock structure pass'}
        if role=='edit':return {key:prompt['submission'][key] for key in ['title','abstract','revision_notes','claim_map']}
        if role=='nature':return mock_review(True)
        if role=='fact_checker':
            d=mock_fact_review();d['checks'][0]['exact_evidence']=self.store.data['sources']['EXAMPLE-S1']['text'];return d
        if role=='committee':
            d=mock_review(False);d['scope_actions']=[{'action':'Keep existing exclusions','kind':'keep_existing_exclusion','blocking':False,'reason':'Already excluded'}]
            d['required_scope_cuts']=['Keep existing exclusions'];return d
        if role=='card':return self.mock_aux('card',prompt)
        if role=='group':return self.mock_aux('group',prompt)
        if role=='meta':return self.mock_aux('meta',prompt)
        if role=='issue_update':return {'assessments':[{'issue_id':i['id'],'status':'unresolved','new_submission_quote':'','source_id':'','reason':'Mock fixture; no scientific closure inferred'} for i in prompt['issues']]}
        if role=='improvement':return {'substantial':True,'criteria':[{'criterion_id':c['id'],'met':True,'evidence_quote':prompt['new']['core_discovery'],'reason':'Mock substantive improvement'} for c in prompt['criteria']],'scientific_change':'Mock','remaining_weaknesses':[]}
        if role=='pair':return {'preferred':'A','ranking':list(prompt['submissions']),'importance_comparison':'Mock','evidence_comparison':'Mock','feasibility_comparison':'Mock','uncertainties':['Not scientifically evaluated']}
        raise ValueError('Unknown role '+role)
    def source_packet(self,actor,stage,full=False):
        key=(actor,stage,full)
        if key in self.packets:return self.packets[key]
        with self.source_lock:
            allowed=self.seed_sources|self.author_source_ids[actor]
            sources=[s for sid,s in self.store.data['sources'].items() if sid in allowed]
        ordered=sorted(sources,key=lambda s:(0 if s['id']=='EXAMPLE-S1' else 1 if s['id']=='EXAMPLE-S2' else 2 if s['id'] in self.author_source_ids[actor] else 3,len(s['text'])))
        chosen=[];size=0;limit=140000 if full else 6500
        for source in ordered:
            record={k:source[k] for k in ['id','paper_id','title','url','location','text'] if k in source}
            cost=len(json.dumps(record,ensure_ascii=False).encode())
            if size+cost<=limit:chosen.append(record);size+=cost
        original={p['id']:p for p in self.literature['papers']};by_paper={s['paper_id']:s for s in sources}
        papers=[]
        for pid in sorted({s['paper_id'] for s in chosen}):
            p=original.get(pid,{'id':pid,'title':by_paper[pid].get('title',''),'url':by_paper[pid].get('url','')})
            papers.append({k:v for k,v in p.items() if k in ['id','title','pmid','aliases','url']})
        return {'papers':papers,'primary_evidence':{'sources':chosen},'limits':['Bounded supplied-source search. Absence is not proof of universal novelty. No inaccessible full text is treated as read.']}
    def freeze_packet(self,actor,stage):
        path=self.run_dir/'stage_inputs'/f'r{stage+1}-writer-{actor+1:02d}.json'
        if path.exists():packets=read(path)
        else:
            packets={'brief':self.source_packet(actor,stage),'full':self.source_packet(actor,stage,True)};save(path,packets)
        self.packets[(actor,stage,False)]=packets['brief'];self.packets[(actor,stage,True)]=packets['full']
    def retrieve(self,actor,stage,label,queries):
        for index,query in enumerate(queries[:self.config['v7']['queries_per_writer']]):
            job=f'r{stage+1}-writer-{actor+1:02d}-{label}-retrieve-{index}'
            def action(query=query,job=job):
                if self.mock:return {'status':'mock','sources':[],'query':query['query'],'errors':[]}
                with self.source_lock:return self.store.retrieve(job,query['query'],limit=self.config['v7']['papers_per_query'])
            result=self.scheduler.execute(job,query,action,recoverable=True)
            self.author_source_ids[actor].update(result['sources'])
    def verify_brief(self,output,packet):
        papers={p['id']:p for p in packet['papers']};sources={s['id']:s for s in packet['primary_evidence']['sources']}
        cited=set()
        for study in output['closest_studies']:
            if study['source_id'] not in sources:raise ValueError('Brief cites an unavailable passage')
            paper_id=sources[study['source_id']]['paper_id']
            if paper_id not in papers:raise ValueError('Brief source has no supplied paper metadata')
            cited.add(paper_id)
        if len(cited)<3:raise ValueError('Research brief needs three distinct supplied relevant papers')
        return output
    def research_brief(self,actor,stage,prior=None,issues=()):
        prefix=f'r{stage+1}-writer-{actor+1:02d}'
        search=self.invoke(prefix+'-search','search',stage,{'instructions':self.prompt('search'),'lens':LENSES[actor],
            'baseline':self.baseline(),'question':prior['question'] if prior else 'Find a distinct research question','open_issues':[i for i in issues if i['owner']=='retrieval' and i['status'] in ['open','needs_explicit_followup']][:6]},SEARCH_SCHEMA,actor)
        self.retrieve(actor,stage,'search',search['queries'])
        if prior:return prior
        packet=self.source_packet(actor,stage)
        brief=self.invoke(prefix+'-brief','brief',stage,{'instructions':self.prompt('brief'),'lens':LENSES[actor],
           'baseline':self.baseline(),'literature':packet},BRIEF_SCHEMA,actor,checker=lambda x:self.verify_brief(x,packet))
        critique=self.invoke(prefix+'-brief-review','brief_review',stage,{'instructions':self.prompt('brief_review'),
            'brief':brief,'baseline':self.baseline(),'literature':packet},BRIEF_REVIEW_SCHEMA)
        if critique['decision']!='ready':
            self.retrieve(actor,stage,'challenge',critique['queries']);packet=self.source_packet(actor,stage)
            brief=self.invoke(prefix+'-brief-revision','brief_revision',stage,{'instructions':self.prompt('brief'),
              'previous':brief,'feedback':critique,'baseline':self.baseline(),'literature':packet},BRIEF_SCHEMA,actor,checker=lambda x:self.verify_brief(x,packet))
            critique=self.invoke(prefix+'-brief-recheck','brief_review',stage,{'instructions':self.prompt('brief_review'),
              'brief':brief,'baseline':self.baseline(),'literature':packet},BRIEF_REVIEW_SCHEMA)
        save(self.run_dir/'briefs'/f'writer-{actor+1:02d}.json',{'brief':brief,'review':critique})
        if critique['decision']=='pivot':raise PausedTask('Research premise still requires a pivot before drafting: '+prefix)
        # Missing lab records remain explicit pending questions, not invented proof.
        save(self.run_dir/'lab_questions'/f'writer-{actor+1:02d}.json',critique['lab_questions'])
        return brief
    @staticmethod
    def structure_issues(draft):
        issues=[]
        if len(draft['abstract'].split())>200:issues.append('Abstract exceeds 200 words')
        if not draft['abstract'].strip():issues.append('Abstract is empty')
        if len(draft['title'].split())>15:issues.append('Title exceeds 15 words')
        if re.search(r'\[\s*\d+\s*\]|https?://|doi:',draft['abstract'],re.I):issues.append('Abstract includes a citation or link')
        if not any(c['status']=='anticipated_result' for c in draft['claim_map']):issues.append('Claim map has no anticipated discovery')
        return issues
    def technical_gate(self,actor,stage,alias,draft):
        def check(number,submission,dep):
            return self.invoke(alias+'-technical-'+str(number),'technical',stage,
              {'instructions':self.prompt('technical'),'submission':{k:submission[k] for k in ['title','abstract','core_discovery','claim_map']},
               'baseline_facts':self.evidence['facts'],'deterministic_issues':self.structure_issues(submission)},TECH_SCHEMA,dependencies=[dep])
        first=check(1,draft,alias+'-draft')
        needs_fix=first['decision']=='revise' or bool(self.structure_issues(draft)) or any(x['severity']=='blocking' for x in first['issues'])
        accepted=alias+'-draft'
        if needs_fix:
            view={k:draft[k] for k in ['title','abstract','core_discovery','claim_map','revision_notes']}
            edit=self.invoke(alias+'-technicalfix','edit',stage,{'instructions':self.prompt('edit'),
               'submission':view,'feedback':first,'deterministic_issues':self.structure_issues(draft),
               'baseline_facts':self.evidence['facts']},EDIT_SCHEMA,actor,dependencies=[alias+'-technical-1'])
            draft=dict(draft);draft.update(edit);accepted=alias+'-technicalfix'
            last=check(2,draft,accepted)
            # Repairs are additional tasks: preserve every original response and
            # completed fingerprint when resuming an older stopped run.
            for recovery in range(1,7):
                if last['decision']=='pass' and not self.structure_issues(draft) and not any(x['severity']=='blocking' for x in last['issues']):break
                job=alias+'-recovery-edit-'+str(recovery)
                repair_prompt={
                    'instructions':self.prompt('edit')+' Return a nonempty claim_map linking the anticipated discovery to existing proposed experiment IDs. Preserve baseline observations exactly. This is a mock finished paper: future findings may be stated as findings, but must be anticipated_result in the sidecar. Do not imply that unmeasured outcomes are completed student results.',
                    'submission':{k:draft[k] for k in ['title','abstract','core_discovery','claim_map','revision_notes','proposed_experiments']},
                    'feedback':last,'deterministic_issues':self.structure_issues(draft),
                    'baseline_facts':self.evidence['facts']}
                if recovery>=4:
                    repair_prompt['instructions']+=' Recovery escalation: write a hypothetical finished-paper abstract, not a grant proposal. State the qualitative anticipated main answer directly (for example, We show...), using only outcomes already anticipated in the supplied plan/claim map. This does not turn them into completed student results: keep anticipated_result provenance outside the abstract. Do not invent numeric effects, confidence intervals or sample sizes. Do not replace the main answer with will test, hypothesize, or estimates unavailable. Keep unresolved resource and quantitative requirements in the existing sidecar.'
                edit=self.invoke(job,'composition' if recovery>=4 else 'edit',stage,repair_prompt,EDIT_SCHEMA,actor,dependencies=[accepted])
                draft=dict(draft);draft.update(edit);accepted=job
                last=check(2+recovery,draft,accepted)
                accepted=alias+'-technical-'+str(2+recovery)
            unresolved=last['decision']!='pass' or self.structure_issues(draft) or any(x['severity']=='blocking' for x in last['issues'])
            if unresolved:
                save(self.run_dir/'structure_failures'/(alias+'.json'),{'review':last,'deterministic':self.structure_issues(draft)})
                edit=self.coordinator_output(alias+'-structure',EDIT_SCHEMA,{
                    'submission':draft,'review':last,'deterministic':self.structure_issues(draft),
                    'baseline_facts':self.evidence['facts']})
                draft=dict(draft);draft.update(edit)
                revision='coordinator-'+hashlib.sha256(json.dumps(edit,sort_keys=True).encode()).hexdigest()[:12]
                last=check(revision,draft,accepted);accepted=alias+'-technical-'+revision
                unresolved=last['decision']!='pass' or self.structure_issues(draft) or any(x['severity']=='blocking' for x in last['issues'])
                if unresolved:
                    save(self.run_dir/'coordinator_handoffs'/(alias+'-structure-recheck.json'),{'review':last,'deterministic':self.structure_issues(draft),'status':'coordinator_fix_failed_recheck'})
                    raise CoordinatorRequired('Coordinator structure fix failed recheck: '+alias)
            elif accepted==alias+'-technicalfix':accepted=alias+'-technical-2'
        else:accepted=alias+'-technical-1'
        unresolved_review=({'review':last,'deterministic':self.structure_issues(draft)} if needs_fix and unresolved else None)
        save(self.run_dir/'gate_decisions'/(alias+'.json'),{'unresolved_technical_review':unresolved_review})
        save(self.run_dir/'submissions'/(alias+'.json'),{'draft':draft,'accepted_gate':accepted,
             'gate_passed':not bool(unresolved_review),'unresolved_technical_review':unresolved_review,'mock':self.mock})
        return draft,accepted
    def official_review(self,actor,stage,alias,draft,gate,previous_reviews=None,open_issues=()):
        result=[]
        for index,reviewer in enumerate(REVIEWERS):
            role='nature' if reviewer=='nature_1' else 'fact_checker' if reviewer=='fact_checker' else 'committee'
            schema=NATURE_SCHEMA if role=='nature' else FACT_SCHEMA if role=='fact_checker' else COMMITTEE_V7
            packet=self.source_packet(actor,stage,full=role=='fact_checker')
            prompt={'instructions':self.contract['editorial'] if role=='nature' else self.prompt('fact') if role=='fact_checker' else self.prompt(reviewer),
              'artifact_context':'Hypothetical finished paper after two remaining PhD years. Results are anticipated unless marked existing in the sidecar.',
              'literature':packet,'abstract_word_limit':200,
              'submission':{k:draft[k] for k in ['title','abstract']} if role=='nature' else draft}
            if role!='nature':prompt['evidence']=self.baseline()
            gate_record=self.run_dir/'gate_decisions'/(alias+'.json')
            if role!='nature' and gate_record.exists() and read(gate_record)['unresolved_technical_review']:
                prompt['unresolved_technical_review']=read(gate_record)['unresolved_technical_review']
            if role=='fact_checker':prompt['primary_evidence']=packet['primary_evidence']
            if previous_reviews:prompt['own_prior_concerns']=previous_reviews[index]['priority_revisions'][:3]
            # No other reviewer grades, competitor drafts, ranks or producing model.
            if role!='nature':prompt['own_open_questions']=[i for i in open_issues if i['reviewer_index']==index][:4]
            def check(output,packet=packet,role=role):
                output,changes=normalize_references(output,packet)
                known={pid for p in packet['papers'] for pid in [p['id'],*p.get('aliases',[])]}
                ids={x['paper_id'] for x in output['literature_assessment']}
                if not self.mock and (len(ids)<2 or not ids<=known):raise ValueError('Official review needs two supplied bibliography IDs')
                if role=='fact_checker':
                    from tournament import verify_fact_review
                    checked,failures=verify_fact_review(output,packet)
                    if failures:raise ValueError('Evidence pointer/source verification failed')
                return output
            suffix=('-gate'+gate.rsplit('-',1)[-1]) if re.search(r'-technical-(?:[6-9]|\d{2,})$',gate) or '-technical-coordinator-' in gate else ''
            result.append(self.invoke(alias+'-official-'+reviewer+suffix,role,stage,prompt,schema,reviewer=reviewer,
                                      dependencies=[gate],checker=check))
        return result
    def prepare_submission(self,alias,actor,stage,prior,standing,old_issues):
        brief=self.research_brief(actor,stage,prior['brief'] if prior else None,old_issues)
        self.freeze_packet(actor,stage)
        old=prior['draft'] if prior else None
        own_reviews=[]
        if prior:
            own_reviews=[{'decision':r['decision'],'priority_revisions':r['priority_revisions'][:3],
                          'scope_actions':r.get('scope_actions',[])} for r in prior['reviews']]
        prior_view={k:old[k] for k in ['title','abstract','core_discovery','proposed_experiments']} if old else None
        baseline={k:self.evidence[k] for k in ['facts','subject_identity','unknowns'] if k in self.evidence}
        packet=self.source_packet(actor,stage)
        # Keep precise original passages; omit unrelated source material from the
        # composition context, not from the underlying evidence store.
        wanted={s['source_id'] for s in brief['closest_studies']}|{'EXAMPLE-S1','EXAMPLE-S2'}
        packet=copy.deepcopy(packet);packet['primary_evidence']['sources']=[s for s in packet['primary_evidence']['sources'] if s['id'] in wanted]
        prompt={'instructions':self.prompt('writer'),'journal_target':'Nature Communications','abstract_word_limit':200,
          'baseline':baseline,'research_brief':brief,'literature':packet,'previous_submission':prior_view,
          'own_feedback':own_reviews,'private_standing':standing,'shared_verified_corrections':self.shared,
          'validation_feedback':draft_checks(old,self.evidence,200) if old else [],
          'planning_rules':'Only E1/E2/E3 are baseline IDs. Future experiment IDs P1-P4. Dependencies are finish-to-start. Quarter milestones 3,6,9,12,15,18,21,24. Preserve a two-year path including repeats, analysis and writing.'}
        draft=self.invoke(alias+'-draft','composition',stage,prompt,DRAFT_SCHEMA,actor)
        accepted,gate=self.technical_gate(actor,stage,alias,draft)
        reviews=self.official_review(actor,stage,alias,accepted,gate,prior['reviews'] if prior else None,old_issues)
        gate_record=read(self.run_dir/'gate_decisions'/(alias+'.json'))
        return {'draft':accepted,'brief':brief,'reviews':reviews,'gate':gate,**gate_record}
    def reconcile_issues(self,actor,stage,previous,submission):
        current=classify_issues(actor,submission['reviews'])
        old={x['id']:dict(x) for x in previous}
        for issue in old.values():
            if issue['owner']=='lab_user':issue['status']='pending_lab_information'
        actionable=[i for i in old.values() if i['owner']!='lab_user' and i['status'] in ['open','needs_explicit_followup','unresolved']][:6]
        if actionable:
            facts=submission['reviews'][1]
            supported={sid for check in facts['checks'] if check['verdict']=='supported' for sid in check['source_ids']}
            assessment=self.invoke(f'r{stage+1}-writer-{actor+1:02d}-issue-followup','issue_update',stage,
              {'instructions':self.prompt('issue_update'),'issues':actionable,
               'new_submission':submission['draft'],'fact_checks':facts['checks'],
               'supported_source_ids':sorted(supported)},ISSUE_UPDATE_SCHEMA)
            strings=[]
            def collect(value):
                if isinstance(value,str):strings.append(value)
                elif isinstance(value,dict):
                    for item in value.values():collect(item)
                elif isinstance(value,list):
                    for item in value:collect(item)
            collect(submission['draft']);allowed={i['id'] for i in actionable}
            for update in assessment['assessments']:
                if update['issue_id'] not in allowed:raise ValueError('Issue follow-up cites an unavailable private issue')
                issue=old[update['issue_id']];status=update['status']
                literal=bool(update['new_submission_quote']) and any(update['new_submission_quote'] in text for text in strings)
                if status in ['addressed_in_plan','out_of_scope'] and not literal:status='unresolved'
                if status=='evidence_added' and update['source_id'] not in supported:status='unresolved'
                issue.update(status=status,resolution=update,
                    evidence_status='planning_response_only' if status in ['addressed_in_plan','out_of_scope'] else 'primary_evidence_added_not_universal_novelty_proof' if status=='evidence_added' else 'unresolved')
        for issue in current:
            # If it recurs in an official review, reopen it regardless of a prior
            # planning closure. History is retained in the per-round files.
            old[issue['id']]=issue
        save(self.run_dir/'issues'/f'r{stage+1}-writer-{actor+1:02d}.json',list(old.values()))
        return list(old.values())
    def export_round(self,stage,ranking,submissions,feedback):
        prefix='r'+str(stage+1)
        lines=['# '+('MOCK ONLY — ' if self.mock else '')+'Round '+str(stage+1),'','Hypothetical papers; anticipated findings are not student results.','',
          '| Candidate | Editorial /5 | Committee /5 |','|---|---:|---:|']
        for row in ranking:lines.append(f"| {row['alias']} | {row['journal_score']} | {row['committee_score']} |")
        for row in ranking:
            data=submissions[row['alias']];d=data['draft'];lines+=['','## '+row['alias']+': '+d['title'],'',d['abstract'],'']
            for reviewer,r in zip(REVIEWERS,data['reviews']):
                lines+=['**'+reviewer+': '+r['decision']+'**','; '.join(r['priority_revisions'])]
        (self.run_dir/(prefix+'-report.md')).write_text('\n'.join(lines)+'\n')
    def cycle(self):
        previous={};private_feedback={};open_issues={};warning_actor=None;warning_criteria=[];warning_old=None
        sizes=self.config['round_sizes'];rounds=len(sizes)-1
        for stage in range(rounds):
            prefix='r'+str(stage+1)
            authors=list(range(4)) if stage==0 else sorted(previous)
            mapping_path=self.run_dir/'private'/(prefix+'-mapping.json')
            if mapping_path.exists():mapping=read(mapping_path)
            else:
                random.Random(1700+stage).shuffle(authors)
                mapping={f'{prefix}-candidate-{i+1:02d}':actor for i,actor in enumerate(authors)};save(mapping_path,mapping)
            if set(mapping.values())!=set(authors):raise ValueError('Round authors changed')
            submissions=self.parallel(mapping,lambda alias,actor:self.prepare_submission(alias,actor,stage,previous.get(actor),private_feedback.get(actor),open_issues.get(actor,[])))
            entries=[(alias,s['draft'],s['reviews'],draft_checks(s['draft'],self.evidence,200)) for alias,s in submissions.items()]
            final=stage==rounds-1
            ranking=rank(entries,self.config['selection'],final=final)
            if not final:
                for row in ranking:
                    row['eligible']=True;row['eligibility_basis']='revision opportunity; not publication or factual endorsement'
            save(self.run_dir/(prefix+'-ranking.json'),ranking)
            all_reviews={a:s['reviews'] for a,s in submissions.items()}
            feedback=standings(ranking,all_reviews,mapping,stage+1)
            for actor,data in feedback.items():
                save(self.run_dir/'private'/f'{prefix}-writer-{actor+1:02d}-standing.json',data)
                open_issues[actor]=self.reconcile_issues(actor,stage,open_issues.get(actor,[]),submissions[next(a for a,v in mapping.items() if v==actor)])
            cards={alias:self.invoke(alias+'-card','card',stage,{'round':stage+1,'task':'Extract scientific hypothesis and decisive experiment, including a real scientific revision if one occurred.','draft':s['draft']},CARD_SCHEMA,
                    dependencies=[s['gate']]) for alias,s in submissions.items()}
            if stage==0:
                warning_alias=ranking[-1]['alias'];warning_actor=mapping[warning_alias]
                warning_criteria=feedback[warning_actor]['warning_criteria'];warning_old=submissions[warning_alias]['draft']
                winners=ranking[:]  # Four advance, even with repairable scientific issues.
            elif stage==1:
                grouped=self.invoke(prefix+'-group','group',stage,{'task':'Group by causal hypothesis and decisive experiment, not terminology. Cover each candidate once.','cards':cards},GROUP_SCHEMA,
                    checker=lambda x:(validate_groups(x,cards) or x))
                alias=next(a for a,v in mapping.items() if v==warning_actor)
                assessment=self.invoke(prefix+'-warning-improvement','improvement',stage,
                    {'instructions':self.prompt('improvement'),'criteria':warning_criteria,'old':warning_old,'new':submissions[alias]['draft']},IMPROVEMENT_SCHEMA,
                    checker=lambda x:(verify_improvement(x,warning_criteria,submissions[alias]['draft']) and x))
                substantial,verified=verify_improvement(assessment,warning_criteria,submissions[alias]['draft'])
                save(self.run_dir/'warning-assessment.json',{'writer':warning_actor,'assessment':assessment,'substantial_verified':substantial,'verified_criteria':verified})
                candidates=copy.deepcopy(ranking)
                if not substantial:
                    for row in candidates:
                        if row['alias']==alias:row['eligible']=False
                winners=select_diverse(candidates,grouped,3)
                if len(winners)!=3:raise PausedTask('Exactly three writers must survive round two; resolve coordinator inconsistency')
                save(self.run_dir/'elimination.json',{'round':2,'eliminated':[a for a in mapping if a not in {r['alias'] for r in winners}],
                    'warned_writer':warning_actor,'warned_writer_recovered':substantial,'basis':'warned proposal must substantively improve; revised rank and diversity otherwise decide'})
            elif not final:
                winners=ranking[:]
            else:
                self.finish(stage,ranking,submissions,mapping)
                self.export_round(stage,ranking,submissions,feedback)
                return
            # Meta-review stays private except source-verified passages. Every
            # writer receives only its own rank, reviews and actionable issues.
            meta_prompt={'task':'Identify shared methodological issues and only source-grounded corrections; do not prescribe a common hypothesis.',
                  'reviews':{alias:[meta_review_brief(r) for r in s['reviews']] for alias,s in submissions.items()},
                  'sources':self.source_packet(next(iter(mapping.values())),stage)['primary_evidence']}
            meta_audit=self.run_dir/'agent_calls'/(prefix+'-meta.json')
            if meta_audit.exists():
                # Preserve the exact source snapshot originally supplied. Earlier
                # launches chose a writer from a transient shuffled author list.
                original=json.loads(read(meta_audit)['messages'][1]['content'])
                if original['task']!=meta_prompt['task'] or original['reviews']!=meta_prompt['reviews']:
                    raise ValueError('Meta-review scientific inputs changed: '+prefix)
                meta_prompt['sources']=original['sources']
            meta=self.invoke(prefix+'-meta','meta',stage,meta_prompt,META_SCHEMA)
            filtered=safe_meta(meta,self.store);self.shared=filtered['shared'];save(self.run_dir/(prefix+'-meta.json'),filtered)
            # Budget includes grouping in every intermediate round; it is useful
            # even when all authors advance, for auditing convergence.
            if stage!=1:
                self.invoke(prefix+'-group','group',stage,{'task':'Audit scientific diversity; cover every proposal exactly once.','cards':cards},GROUP_SCHEMA,
                    checker=lambda x:(validate_groups(x,cards) or x))
            save(self.run_dir/(prefix+'-advancement.json'),winners)
            self.export_round(stage,ranking,submissions,feedback)
            previous={mapping[row['alias']]:submissions[row['alias']] for row in winners}
            private_feedback={actor:feedback[actor] for actor in previous}
            print(prefix+': official reviews complete; advancing '+str(len(previous)),flush=True)
    def finish(self,stage,ranking,submissions,mapping):
        labels_path=self.run_dir/'private/final-labels.json'
        if labels_path.exists():labels=read(labels_path)
        else:
            aliases=sorted(submissions);random.Random(919).shuffle(aliases);labels=dict(zip(['A','B','C'],aliases));save(labels_path,labels)
        if set(labels.values())!=set(submissions):raise ValueError('Final blinded mapping changed')
        prompt={'task':'Compare all three hypothetical papers. Rank by scientific value, evidence and two-year feasibility. Distinguish full acceptance from a conditional project recommendation. None is valid. Do not override a documented contradiction.',
          'baseline_facts':self.evidence['facts'],
          'submissions':{label:{'title':submissions[a]['draft']['title'],'abstract':submissions[a]['draft']['abstract'],
             'experiments':submissions[a]['draft']['proposed_experiments']} for label,a in labels.items()}}
        assessment=self.invoke('r6-final-comparison','pair',stage,prompt,FINAL_SCHEMA,
             checker=lambda x:x if set(x['ranking'])==set(labels) and len(x['ranking'])==len(labels) and x['preferred'] in {*labels,'none'} else (_ for _ in ()).throw(ValueError('Invalid final labels/ranking')))
        judgments={row['alias']:verdict(row,submissions[row['alias']]['reviews']) for row in ranking}
        for alias,submission in submissions.items():
            if submission.get('unresolved_technical_review'):
                judgments[alias].update(full_pass=False,conditional_recommendation_eligible=False,
                    unresolved_technical_review=submission['unresolved_technical_review'])
        order=[labels[label] for label in assessment['ranking']]
        qualified=[a for a in order if judgments[a]['full_pass']]
        conditional=[a for a in order if judgments[a]['conditional_recommendation_eligible']]
        if assessment['preferred']=='none':selected=None;status='no_recommendation'
        elif qualified:selected=qualified[0];status='qualified_recommendation'
        elif conditional:selected=conditional[0];status='conditional_recommendation'
        else:selected=None;status='no_recommendation'
        preferred_alias=labels.get(assessment['preferred'])
        outcome={'status':status,'selected':selected,'verdicts':judgments,'comparison':assessment,
          'comparison_preferred':preferred_alias,
          'selection_policy':'Highest-ranked fully qualified candidate, else highest-ranked conditionally eligible candidate; none if the comparator rejects all. A preferred but ineligible candidate is not approved.',
          'selection_disagrees_with_blind_preference':bool(selected and selected!=preferred_alias),
          'note':'Conditional recommendation is not factual clearance, journal acceptance or guaranteed feasibility.'}
        save(self.run_dir/'outcome.json',outcome);save(self.run_dir/'completion.json',{'status':'completed','mock':self.mock,'rounds':6})
        lines=['# '+('MOCK ONLY — ' if self.mock else '')+'Final hypothetical papers','','All findings are anticipated unless explicitly present in the confirmed student baseline.','',
               'Outcome: '+status,'Selected: '+str(selected),'']
        for row in ranking:
            a=row['alias'];d=submissions[a]['draft'];j=judgments[a]
            lines+=['## '+a+': '+d['title'],'',d['abstract'],'',json.dumps(j,indent=2),'',
                    '[Full draft and plan](submissions/'+a+'.json)','']
        (self.run_dir/'FINALISTS.md').write_text('\n'.join(lines)+'\n')
        budget=read(self.budget.path) if self.budget.path.exists() else {'entries':[]}
        spent=sum(e.get('accounted_usd',e['reserved_usd']) for e in budget['entries'])
        questions=[]
        for actor in mapping.values():
            path=self.run_dir/'lab_questions'/f'writer-{actor+1:02d}.json'
            if path.exists():questions.extend(read(path))
            path=self.run_dir/'issues'/f'r{stage+1}-writer-{actor+1:02d}.json'
            if path.exists():questions.extend(i['text'] for i in read(path) if i['owner']=='lab_user')
        questions=list(dict.fromkeys(questions))
        (self.run_dir/'QUESTIONS.md').write_text('# Pending lab information\n\n'+('\n'.join('- '+q for q in questions) if questions else 'No lab questions were requested in this run.')+'\n')
        (self.run_dir/'REPORT.md').write_text('# '+('MOCK ONLY — ' if self.mock else '')+'Research-first tournament\n\n'+status+'\n\n[Final abstracts and separate verdicts](FINALISTS.md)\n\n[Pending lab information](QUESTIONS.md)\n\nConservative accounting: $'+str(round(spent,4))+'\n')
        print('Completed six cycles: '+status,flush=True)
