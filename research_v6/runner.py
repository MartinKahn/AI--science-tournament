"""Version-six orchestration; new run directories only, legacy artifacts untouched."""
import json
import random
from pathlib import Path
from api_agents import actor_for_job,roster
from api_budget import input_token_bound,BudgetExceeded
from prospective import (DRAFT_SCHEMA,NATURE_SCHEMA,COMMITTEE_SCHEMA,FACT_SCHEMA,REVIEWERS,
    REVIEW_SCHEMAS,LENSES,draft_prompt,review_prompt,rank,mock_draft,mock_review,mock_fact_review)
from tournament import Runner,save,read,digest,draft_checks,validate
from research_v6.scheduler import Scheduler,PausedTask
from research_v6.claims import EvidenceStore
from research_v6.design import (CARD_SCHEMA,GROUP_SCHEMA,META_SCHEMA,PAIR_SCHEMA,validate_groups,select_diverse,safe_meta,meta_review_brief,revision_path_exists)
from research_v6.budget_plan import preflight,envelope_cost
from research_v6.policy import request_spec,earned_repair_allowance

class ResearchRunner(Runner):
    def __init__(self,config,run_dir,mock=False):
        super().__init__(config,run_dir,mock)
        self.editorial=read(self.run_dir/'manifest.json').get('v6_contract',{}).get('editorial')
        self.scheduler=Scheduler(self.run_dir/'tasks')
        self.store=EvidenceStore(self.run_dir/'evidence-store.json')
        self.store.ingest(self.literature.get('primary_evidence',{}).get('sources',[]))
        self.current_packets=None
        self.retry_unknown=False
        self.shared={'verified_passages':[],'general_reminders':[]}
    def task_progress(self,job_id,state,**metadata):
        self.scheduler.progress(job_id.replace('-repair1-','-'),state,**metadata)
    def make_conversation(self,job_id,prompt,schema):
        actor_id=actor_for_job(job_id,self.run_dir,self.config,schema);actor=self.actors[actor_id]
        role='writer' if schema==DRAFT_SCHEMA else 'nature' if schema==NATURE_SCHEMA else 'fact_checker' if schema==FACT_SCHEMA else 'committee'
        stage=int(job_id.split('-')[0][1:])-1
        spec=request_spec(self.config,role,stage,actor=actor_id,reviewer=actor_id if schema!=DRAFT_SCHEMA else None)
        current=prompt
        message=json.dumps(current,ensure_ascii=False,separators=(',',':'))
        if schema==DRAFT_SCHEMA:
            from review_grounding import compact_draft_schema
            messages=[{'role':'system','content':actor['system_prompt']},{'role':'user','content':message}]
            estimate={'input':messages,'text':{'format':{'type':'json_schema','name':'tournament_output','strict':True,'schema':compact_draft_schema(schema)}}}
            if input_token_bound(estimate)-4096>spec['max_input_bytes']-512:
                current=json.loads(json.dumps(prompt))
                for review in current.get('reviews') or []:
                    for key,value in list(review.items()):
                        if isinstance(value,str) and len(value)>350:review[key]=value[:350]+' [abridged]'
                        elif isinstance(value,list):review[key]=[str(x)[:200] for x in value]
                for review in current.get('reviews') or []:
                    review.pop('quality_assessment',None)
                    review.pop('feasibility_assessment',None)
                prior=current.get('previous_draft')
                if prior:
                    prior.pop('revision_notes',None)
                    prior.pop('scope_cuts',None)
                current['context_notice']='Review narratives were abridged to fit the planned input allowance. Decisions, scores, baseline facts, own experiments and validation feedback are preserved; original reviews remain in the run audit.'
                message=json.dumps(current,ensure_ascii=False,separators=(',',':'))
                def context_size():
                    estimate['input'][1]['content']=json.dumps(current,ensure_ascii=False,separators=(',',':'))
                    return input_token_bound(estimate)-4096
                if context_size()>spec['max_input_bytes']-512:
                    for review in current.get('reviews') or []:review.pop('scores',None)
                    current['context_notice']='Review prose abridged; numeric grades and redundant prior summaries may be omitted. Decisions, concrete revision requests, baseline facts, own abstract/experiments and validation feedback remain. Full originals are saved in the run audit.'
                if context_size()>spec['max_input_bytes']-512 and prior:
                    prior.pop('novelty_vs_prior_work',None)
                message=json.dumps(current,ensure_ascii=False,separators=(',',':'))
        return {'actor_id':actor_id,'job_id':job_id,'model':spec['model'],'reasoning_effort':spec['reasoning_effort'],
                'messages':[{'role':'system','content':actor['system_prompt']}, {'role':'user','content':message}],
                'user_message':message,'identity_version':'v6-fresh-own-context',
                'history_policy':'Only own current draft and own reviews supplied; no competitors or cross-candidate review history',
                'output_token_limit':spec['max_output_tokens'],'input_byte_limit':spec['max_input_bytes'],'budget_role':role}
    def validate_payload(self,job_id,payload,audit):
        # Same serialization and framing bound used by the billing guard.
        size=input_token_bound(payload)-4096
        limit=audit['input_byte_limit']*(2 if '-repair1-' in job_id else 1)+(8192 if '-repair1-' in job_id else 0)
        if size>limit:
            raise PausedTask(f'{job_id}: input {size} bytes exceeds planned envelope {limit}; no request sent')
        if '-repair1-' in job_id:
            from api_budget import reservation_usd
            allowance=earned_repair_allowance(self.config,self.run_dir)
            pricing=self.config['pricing_profiles'][payload['model']]
            save(self.run_dir/'repair_budget_checks'/(job_id+'.json'),allowance)
            if allowance['committed_repairs_usd']+reservation_usd(payload,pricing)>allowance['allowance_usd']:
                raise BudgetExceeded('Dedicated repair allowance and earned unused base credit exhausted; future base-stage budget remains protected')
    def api(self,prompt,schema,job_id):
        response=self.run_dir/'responses'/(job_id+'.json')
        if not response.exists() and self.budget.path.exists():
            uncertain=[e for e in read(self.budget.path)['entries'] if e['job_id']==job_id and e['status']=='pending_or_unknown']
            if uncertain and not self.retry_unknown:
                raise PausedTask('Unknown billing outcome for '+job_id+'; reconcile it or explicitly use --retry-unknown. No automatic duplicate request was sent.')
        try:return super().api(prompt,schema,job_id)
        finally:
            if self.budget.path.exists():
                main=job_id.replace('-repair1-','-')
                entries=[e for e in read(self.budget.path)['entries'] if e['job_id'].replace('-repair1-','-')==main]
                self.scheduler.progress(main,budget_reservations=entries)
    def validate_review(self,output,schema):
        validate(output,schema)
        packet=self.literature_packet(full=schema==FACT_SCHEMA)
        sources=packet['primary_evidence']['sources']
        valid={pid for paper in packet['papers'] for pid in [paper['id'],*paper.get('aliases',[])]}
        ids={x['paper_id'] for x in output['literature_assessment']}
        from tournament import ReviewFormatError,verify_fact_review
        if len(ids)<2 or not ids<=valid:raise ReviewFormatError('literature_assessment requires two available paper IDs; unknown IDs: '+str(sorted(ids-valid)))
        if schema==FACT_SCHEMA:
            _,issues=verify_fact_review(output,{'primary_evidence':{'sources':sources}})
            if issues:raise ReviewFormatError('Primary source/quote validation failed: '+json.dumps(issues))
    def call(self,job_id,prompt,schema):
        recoverable=any((self.run_dir/folder/(job_id+'.json')).exists() for folder in ['jobs','responses'])
        # A repair response also permits safe recovery without replaying its API call.
        if schema!=DRAFT_SCHEMA:
            repair=job_id.rsplit('-',1)[0]+'-repair1-'+job_id.rsplit('-',1)[1]
            recoverable=recoverable or (self.run_dir/'responses'/(repair+'.json')).exists()
        def action():
            try:output=super(ResearchRunner,self).call(job_id,prompt,schema)
            except (RuntimeError,ValueError) as exc:
                if schema!=DRAFT_SCHEMA or not ('incomplete' in str(exc) or 'complete' in str(exc) or isinstance(exc,ValueError)):raise
                output=self.repair_writer(job_id,prompt,schema,str(exc))
            if schema==FACT_SCHEMA:
                for check in output['checks']:
                    for sid in check['source_ids']:
                        if sid in self.store.data['sources']:
                            self.store.add_claim(check['claim'],sid,check['exact_evidence'],scope=check['scope'],verdict=check['verdict'])
            return output
        dependencies=[]
        if schema!=DRAFT_SCHEMA:
            dependencies=[job_id.rsplit('-',1)[0]+'-draft']
        elif int(job_id.split('-')[0][1:])>1:
            dependencies=['r'+str(int(job_id.split('-')[0][1:])-1)+'-meta']
        return self.scheduler.execute(job_id,{'prompt':prompt,'schema':schema},action,dependencies=dependencies,
                    recoverable=recoverable,retry_unknown=self.retry_unknown)
    def repair_writer(self,job_id,prompt,schema,error):
        main=self.make_conversation(job_id,prompt,schema)
        repair=job_id.removesuffix('-draft')+'-repair1-draft'
        audit=json.loads(json.dumps(main));audit['job_id']=repair;audit['repair_of']=job_id
        if self.config.get('research_compute'):
            # Initial scientific seeds already exist. Complete their schema/plan
            # using the authorized revision engine rather than redoing expensive
            # high-effort idea generation. Record the model change explicitly.
            completion=request_spec(self.config,'writer',1,actor=main['actor_id'])
            audit.update(model=completion['model'],reasoning_effort=completion['reasoning_effort'],output_token_limit=completion['max_output_tokens'],
                         seed_model=main['model'],repair_strategy='complete_saved_seed_with_revision_engine')
        original=self.run_dir/'responses'/(job_id+'.json')
        if original.exists():
            raw=read(original);partial=''.join(c.get('text','') for item in raw.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text').rstrip()
            if partial:audit['messages'].append({'role':'assistant','content':partial[:20000]})
        audit['messages'].append({'role':'user','content':'GAME MASTER: The prior draft was incomplete or malformed. One repair attempt. Regenerate the complete JSON, not a continuation. Keep each planning field to one concise sentence, use at most three experiments and finish well below 4500 tokens. Do not pad with blank lines. Preserve all supplied baseline facts and the central hypothesis from the saved seed. Do not restart scientific idea generation. Error: '+error})
        self.task_progress(job_id,'repair_requested',validation_error=error)
        save(self.run_dir/'agent_calls'/(repair+'.json'),audit)
        output,usage=self.api(prompt,schema,repair);validate(output,schema)
        fingerprint=digest({'prompt':prompt,'schema':schema,'config':self.config,'mock':self.mock,'agent_call':main})
        save(self.run_dir/'jobs'/(job_id+'.json'),{'fingerprint':fingerprint,'output':output,'usage':usage,'mock':self.mock,'repaired_by':repair})
        save(self.run_dir/'review_repairs'/(job_id+'.json'),{'status':'repaired','kind':'writer','error':error,'accepted_job_id':repair})
        self.task_progress(job_id,'validated');return output
    def aux(self,job_id,role,prompt,schema):
        spec=request_spec(self.config,role,int(job_id.split('-')[0][1:])-1)
        audit={'actor_id':role,'job_id':job_id,'model':spec['model'],'reasoning_effort':spec['reasoning_effort'],
               'messages':[{'role':'system','content':'You are an independent research coordinator. Return only the requested JSON. Use only supplied evidence. Keep answers concise.'},
                           {'role':'user','content':json.dumps(prompt,ensure_ascii=False,separators=(',',':'))}],
               'output_token_limit':spec['max_output_tokens'],'input_byte_limit':spec['max_input_bytes'],'budget_role':role}
        def action():
            save(self.run_dir/'agent_calls'/(job_id+'.json'),audit)
            if self.mock:output=self.mock_aux(role,prompt)
            else:output,_=self.api(prompt,schema,job_id)
            validate(output,schema)
            if role=='group':validate_groups(output,prompt['cards'])
            if role=='pair' and output['preferred'] not in {*prompt['submissions'],'neither'}:raise ValueError('Pairwise adjudicator selected an unavailable label')
            return output
        stage=job_id.split('-')[0]
        if role=='card':dependencies=[job_id.removesuffix('-card')+'-draft']
        else:
            mapping=read(self.run_dir/'private'/(stage+'-mapping.json'))
            if role=='group':dependencies=[alias+'-card' for alias in mapping]
            elif role in ('meta','pair'):dependencies=[alias+'-'+reviewer for alias in mapping for reviewer in REVIEWERS]
            else:dependencies=[]
        return self.scheduler.execute(job_id,{'prompt':prompt,'schema':schema},action,dependencies=dependencies,
            recoverable=(self.run_dir/'responses'/(job_id+'.json')).exists(),retry_unknown=self.retry_unknown)
    def mock_aux(self,role,prompt):
        if role=='card':
            draft=prompt['draft']
            return {'hypothesis':draft['core_discovery'],'mechanism':'To be tested','decisive_experiment':draft['proposed_experiments'][0]['approach'] if draft['proposed_experiments'] else 'Not supplied',
                    'competing_explanation':'Alternative cellular mechanism','revision_strategy':'initial' if prompt['round']==1 else 'strengthen','scientific_change':'Mock fixture; no scientific endorsement'}
        if role=='group':return {'groups':[{'id':'g'+str(i),'members':[alias],'causal_distinction':card['hypothesis']} for i,(alias,card) in enumerate(prompt['cards'].items())]}
        if role=='meta':return {'methodological_weaknesses':['Distinguish causal tests from morphology'],'unresolved_disagreements':[], 'proposed_corrections':[],'evidence_requests':[]}
        return {'preferred':'neither','importance_comparison':'Offline fixture','evidence_comparison':'Not scientifically adjudicated','feasibility_comparison':'Not scientifically adjudicated','uncertainties':['Mock run']}
    def baseline(self):
        return {key:self.evidence[key] for key in ['version','scope','facts','unknowns','subject_identity','interpretation_constraints'] if key in self.evidence}
    def literature_packet(self,full=False):
        if self.current_packets is not None:return self.current_packets['full' if full else 'brief']
        sources=list(self.store.data['sources'].values())
        if full:
            # Respect the fact-checker envelope while retaining complete source passages.
            chosen=[];size=0
            for source in sources:
                record={k:source[k] for k in ['id','paper_id','url','location','text'] if k in source}
                cost=len(json.dumps(record,ensure_ascii=False).encode())
                if size+cost<=150000:chosen.append(record);size+=cost
        else:
            chosen=[];size=0
            # Prioritize synthetic example control passages first.
            requested={sid for request in self.store.data['requests'].values() for sid in request.get('sources',[])}
            def relevance(source):
                text=(source.get('title','')+' '+source['text']).lower()
                score=sum(text.count(word) for word in ['control','replication','benchmark','measurement','uncertainty'])
                return (0 if source['id']=='EXAMPLE-S1' else 1 if source['id']=='EXAMPLE-S2' else 2 if source['id'].startswith('EXAMPLE') else 3 if source['id'] in requested else 4,-score,len(source['text']))
            ordered=sorted(sources,key=relevance)
            for source in ordered:
                if len(chosen)>=8:break
                record={k:source[k] for k in ['id','paper_id','url','location','text'] if k in source}
                cost=len(json.dumps(record,ensure_ascii=False).encode())
                if size+cost<=4500:chosen.append(record);size+=cost
        ids={s['paper_id'] for s in chosen}
        original={p['id']:p for p in self.literature['papers']}
        by_paper={source['paper_id']:source for source in sources}
        papers=[{k:v for k,v in original.get(pid,{'id':pid,'title':by_paper[pid].get('title',''),'url':by_paper[pid].get('url','')}).items() if k in ['id','title','pmid','doi','url','aliases']} for pid in sorted(ids)]
        return {'papers':papers,'primary_evidence':{'sources':chosen},'limits':['Only the supplied passages were retrieved. Passage verification does not establish that a claim is supported. Omitted sources and inaccessible full text remain gaps.'],'reviewer_reports':{}}
    @staticmethod
    def brief_reviews(reviews):
        return [{k:([str(x)[:400] for x in v[:4]] if isinstance(v,list) else v) for k,v in review.items() if k in ['decision','scores','priority_revisions','story_gaps','critical_path','required_scope_cuts','contradictions','missing_primary_sources','quality_assessment','feasibility_assessment']} for review in reviews]
    def report(self,prefix,ranking,drafts,blocked=False,final=False):
        super().report(prefix,ranking,drafts,blocked=blocked,final=final)
        if self.editorial:
            path=self.run_dir/(prefix+'-report.md')
            path.write_text(path.read_text().replace('The Nature reviewer','The '+self.editorial['journal']+' reviewer'))

    def cycle(self):
        sizes=self.config['round_sizes'];previous={};winners=[]
        for stage in range(len(sizes)-1):
            prefix='r'+str(stage+1)
            packet_path=self.run_dir/'stage_inputs'/(prefix+'-literature.json')
            self.current_packets=None
            if packet_path.exists():self.current_packets=read(packet_path)
            else:
                packets={'brief':self.literature_packet(),'full':self.literature_packet(full=True)}
                save(packet_path,packets);self.current_packets=packets
            authors=list(range(sizes[0])) if stage==0 else list(previous)
            random.Random(600+stage).shuffle(authors)
            mapping={f'{prefix}-candidate-{i+1:02d}':a for i,a in enumerate(authors)}
            save(self.run_dir/'private'/(prefix+'-mapping.json'),mapping)
            tasks={}
            for alias,author in mapping.items():
                old,reviews=previous.get(author,(None,None))
                old_context={k:v for k,v in old.items() if k not in ['milestones','baseline_use','claim_map']} if old else None
                prompt=draft_prompt(self.baseline(),LENSES[author],self.config['word_limit'],old_context,self.brief_reviews(reviews) if reviews else None,self.literature_packet())
                if self.editorial:
                    prompt['journal_target']=self.editorial['journal']
                    if self.editorial.get('writer_instructions'):prompt['instructions']=self.editorial['writer_instructions']
                prompt['planning_length_instruction']='Use at most four proposed experiments and concise sidecar entries within the schema limits. Group related controls coherently. Keep the abstract journal-ready. Use plain ASCII notation for units and symbols.'
                prompt['revision_instruction']='Explicitly strengthen, pivot, simplify or resolve an evidence conflict. Explain scientific changes in revision_notes. Propose a decisive discriminator and a competing explanation. Do not just polish wording.'
                if old is not None:
                    prompt['validation_feedback']=draft_checks(old,self.evidence,self.config['word_limit'])
                    prompt['scope_revision_requirement']='Resolve the committee scope cuts first. Remove or de-emphasize essential headline claims that require too many unconfirmed platforms. Preserve the decisive controls and explain the scientific claim after each scope cut. Advancement permits revision; it does not endorse feasibility.'
                    prompt['planning_schema_notes']='Fix every validation issue: baseline IDs only E1, E2, E3; future experiment IDs P1 through P4. Dependencies mean finish-to-start: an experiment may begin only after every dependency ends. Separate enabling validation from ongoing measurements where necessary. These repairs are required before final acceptance.'
                prompt['shared_feedback']=self.shared
                tasks[alias]=(alias+'-draft',prompt,DRAFT_SCHEMA)
            drafts=self.batch(tasks)
            review_tasks={}
            # Central evidence and feasibility checks are scheduled before prestige
            # judgments. Later rounds focus each reviewer on its own open issues.
            for reviewer in ['fact_checker','committee_1','committee_2','nature_1']:
                for alias,draft in drafts.items():
                    prompt=review_prompt(self.baseline(),draft,reviewer,self.config['word_limit'],self.literature_packet(full=reviewer=='fact_checker'))
                    if reviewer=='nature_1' and self.editorial:
                        prompt['instructions']=self.editorial['instructions']
                        prompt['role']=self.config['editorial']['persona']
                    if reviewer=='nature_1':
                        prompt['primary_source_passages']=self.literature_packet()['primary_evidence']['sources']
                    prior=previous.get(mapping[alias])
                    if prior:
                        own=prior[1][list(REVIEWERS).index(reviewer)]
                        prompt['focus_questions']=own['priority_revisions'][:4]
                        prompt['review_instruction']='Assess whether your prior concerns were resolved; retain the full rubric. Do not reward wording changes without scientific improvement.'
                    review_tasks[alias+'/'+reviewer]=(alias+'-'+reviewer,prompt,REVIEW_SCHEMAS[reviewer])
            reviews=self.batch(review_tasks)
            entries=[(alias,d,[reviews[alias+'/'+r] for r in REVIEWERS],draft_checks(d,self.evidence,self.config['word_limit'])) for alias,d in drafts.items()]
            final=stage==len(sizes)-2;ranking=rank(entries,self.config['selection'],final=final)
            if not final:
                for row in ranking:
                    # Journal rejection can motivate revision; baseline integrity and
                    # a basic feasible path cannot be bypassed for diversity alone.
                    own=[reviews[row['alias']+'/'+role] for role in ['committee_1','committee_2']]
                    baseline_compatible=not any(review['baseline_contradictions'] for review in own)
                    row['eligible']=revision_path_exists(own)
                    row['required_repairs']=row['local_issues']+[issue for review in own for key in ['provenance_errors','unsupported_resource_assumptions'] for issue in review[key]]
                    row['eligibility_basis']='baseline-compatible conditional feasibility permits scope revision; strict feasibility required only for final acceptance'
            save(self.run_dir/(prefix+'-ranking.json'),ranking)
            cards={alias:self.aux(alias+'-card','card',{'round':stage+1,'task':'Extract the causal hypothesis, mechanism, decisive discriminator, competing explanation and actual revision strategy. Report missing elements rather than inventing them.','draft':d},CARD_SCHEMA) for alias,d in drafts.items()}
            save(self.run_dir/(prefix+'-cards.json'),cards)
            if final:
                label_path=self.run_dir/'private/final-pair-labels.json'
                if label_path.exists():labels=read(label_path)
                else:
                    aliases=sorted(drafts);random.Random(991).shuffle(aliases)
                    labels=dict(zip(['A','B'],aliases))
                    save(label_path,labels)
                if set(labels.values())!=set(drafts):raise ValueError('Final comparison candidate mapping changed')
                pair=self.aux(prefix+'-pair','pair',{'task':'Compare blinded hypothetical papers. Keep importance, evidential defensibility and 24-month feasibility separate. Prefer neither if appropriate. This comparison cannot override factual/feasibility gates.', 'baseline_facts':self.evidence['facts'],'verified_passages':self.literature_packet()['primary_evidence']['sources'][:3], 'submissions':{label:{'abstract':drafts[a]['abstract'],'core_discovery':drafts[a]['core_discovery'],'plan':drafts[a]['proposed_experiments']} for label,a in labels.items()}},PAIR_SCHEMA)
                save(self.run_dir/'private/final-pair-labels.json',labels);save(self.run_dir/'final-comparison.json',pair)
                eligible=[row for row in ranking if row['eligible']]
                preferred=labels.get(pair['preferred'])
                # Pairwise preference only orders candidates already passing all gates.
                eligible.sort(key=lambda row:row['alias']!=preferred)
                winners=eligible[:1] if pair['preferred']!='neither' else []
            else:
                grouped=self.aux(prefix+'-group','group',{'task':'Group proposals by central causal explanation and decisive experiment, not terminology. Cover every alias exactly once; justify causal distinctions.','cards':cards},GROUP_SCHEMA)
                save(self.run_dir/(prefix+'-groups.json'),grouped)
                winners=select_diverse(ranking,grouped,sizes[stage+1])
                meta=self.aux(prefix+'-meta','meta',{'task':'Identify recurring methodological weaknesses, disagreements and targeted PubMed queries for central evidence gaps. Do not introduce scientific answers or quote competing drafts. Proposed corrections require exact source passages.','reviews':{k:meta_review_brief(v) for k,v in reviews.items()},'sources':self.literature_packet()['primary_evidence']},META_SCHEMA)
                filtered=safe_meta(meta,self.store);save(self.run_dir/(prefix+'-meta.json'),filtered);self.shared=filtered['shared']
                if self.config['v6']['retrieval_enabled'] and not self.mock:
                    requests=sorted(filtered['private']['evidence_requests'],key=lambda x:x['centrality']!='central')[:self.config['v6']['retrieval_requests_per_round']]
                    for i,request in enumerate(requests):
                        self.scheduler.execute(prefix+'-retrieve-'+str(i),request,lambda request=request,i=i:self.store.retrieve(prefix+'-'+str(i),request['query'],limit=self.config['v6']['retrieval_papers_per_request']),recoverable=True)
                    # Retrieval can inform the next round only, preserving previous inputs.
            save(self.run_dir/(prefix+'-advancement.json'),winners)
            self.report(prefix,ranking,drafts,final=final and bool(winners),blocked=not winners)
            print(prefix+': completed; advanced '+str(len(winners)),flush=True)
            if not winners:
                self.no_winner(prefix,ranking);save(self.run_dir/'completion.json',{'status':'no_qualified_winner','round':stage+1,'mock':self.mock});return
            previous={mapping[row['alias']]:(drafts[row['alias']],[reviews[row['alias']+'/'+r] for r in REVIEWERS]) for row in winners}
        winner=winners[0]['alias'];save(self.run_dir/'winner.json',{'alias':winner,'draft':drafts[winner],'mock':self.mock,'artifact_status':'hypothetical_end_of_phd_paper'})
        save(self.run_dir/'completion.json',{'status':'completed','mock':self.mock})
