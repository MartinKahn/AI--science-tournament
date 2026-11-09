#!/usr/bin/env python3
"""Research-first tournament: four survive round one, three survive round two."""
import argparse
import fcntl
from pathlib import Path
from tournament import ROOT,read,save
from prospective import task_contract
from api_agents import roster
from research_v7.budget import preflight
from research_v7.contracts import PROMPTS,SCHEMAS
from research_v7.runner import TournamentV7

def contract(config):
    prompts=dict(PROMPTS)
    prompts.update(writer=(ROOT/'prompts/writer_v7.md').read_text(),
      fact=(ROOT/'prompts/fact_v7.md').read_text(),
      committee_1=(ROOT/'prompts/committee_v7.md').read_text(),
      committee_2=(ROOT/'prompts/committee_v7.md').read_text())
    return {'version':'research-v7','prompts':prompts,'schemas':SCHEMAS,
      'editorial':(ROOT/config['editorial']['instructions_file']).read_text(),
      'operational_style':read(ROOT/'operational_style.json')}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','prepare','mock','run'])
    parser.add_argument('--config',type=Path,default=ROOT/'config.v7.json')
    parser.add_argument('--run-dir',type=Path,default=ROOT/'runs/research-v7')
    parser.add_argument('--retry-unknown',action='store_true')
    parser.add_argument('--profile',type=Path,default=ROOT/'examples/synthetic',help='Directory containing evidence.json and literature.json; keep real profiles outside this checkout')
    args=parser.parse_args();config=read(args.config);plan=preflight(config)
    if args.command=='plan':print(__import__('json').dumps(plan,indent=2));return
    if args.command=='run':
        if not config.get('allow_paid_run'):parser.error('Paid execution disabled: implementation/rehearsal only')
        if not plan['fits']:parser.error('Full bounded schedule exceeds the spending cap; no paid requests sent')
    args.run_dir.mkdir(parents=True,exist_ok=True);lock=(args.run_dir/'.run.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.error('Another process owns this run')
    literature=read(args.profile/'literature.json');evidence=read(args.profile/'evidence.json')
    manifest={'config':config,'evidence':evidence,'literature':literature,'mock':args.command=='mock',
      'task_contract':task_contract(),'agent_roster':roster(config),'v7_contract':contract(config)}
    path=args.run_dir/'manifest.json'
    if path.exists():
        if read(path)!=manifest:parser.error('Immutable run inputs changed; use a new v7 directory')
    else:
        save(path,manifest);save(args.run_dir/'evidence.lock.json',evidence);save(args.run_dir/'literature.lock.json',literature)
    save(args.run_dir/'budget-plan.json',plan)
    if args.command=='prepare':print('V7 prepared. Paid launch remains subject to budget and explicit enablement.');return
    runner=TournamentV7(config,args.run_dir,mock=args.command=='mock');runner.retry_unknown=args.retry_unknown
    save(args.run_dir/'execution-status.json',{'status':'running','mock':args.command=='mock'})
    try:runner.cycle()
    except Exception as exc:
        save(args.run_dir/'execution-status.json',{'status':'needs_coordinator_repair' if type(exc).__name__=='CoordinatorRequired' else 'paused','error_type':type(exc).__name__,'reason':str(exc)})
        raise
    save(args.run_dir/'execution-status.json',{'status':'completed','mock':args.command=='mock'})

if __name__=='__main__':main()
