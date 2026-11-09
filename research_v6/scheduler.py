"""Durable tasks, explicit interruption handling, and immutable task inputs."""
import hashlib
import json
from pathlib import Path
import threading
import time
from tournament import save, read

LOCK=threading.RLock()
class PausedTask(RuntimeError):pass
class CoordinatorRequired(PausedTask):
    """A technical repair needs the main agent; it is not scientific rejection."""
    pass

class Scheduler:
    def __init__(self,root):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
    def progress(self,job_id,state=None,**metadata):
        path=self.root/(job_id+'.json')
        if not path.exists():return
        with LOCK:
            task=read(path)
            if state:
                task['state']=state;task['history'].append({'state':state,'at':time.time()})
            task.update(metadata);task['updated_at']=time.time();save(path,task)
    def execute(self,job_id,inputs,action,dependencies=(),recoverable=False,retry_unknown=False):
        path=self.root/(job_id+'.json')
        fingerprint=hashlib.sha256(json.dumps({'inputs':inputs,'dependencies':list(dependencies)},sort_keys=True).encode()).hexdigest()
        with LOCK:
            state=read(path) if path.exists() else {'id':job_id,'fingerprint':fingerprint,'state':'queued','dependencies':list(dependencies),'history':[]}
            save(path,state)
            if state['fingerprint']!=fingerprint:raise ValueError('Task inputs changed: '+job_id)
            if state['state']=='completed':return state['result']
            for dep in dependencies:
                p=self.root/(dep+'.json')
                if not p.exists() or read(p)['state']!='completed':raise PausedTask('Unfinished dependency: '+dep)
            if state['state'] in ('running','received','validated','repair_requested','paused_unknown') and not recoverable and not retry_unknown:
                state['state']='paused_unknown';save(path,state)
                raise PausedTask('Interrupted task has no durable response: '+job_id+'. Reconcile charges or explicitly authorize retry_unknown; existing reservations remain held.')
            state.update(state='running',updated_at=time.time())
            state['history'].append({'state':'running','at':time.time(),'explicit_unknown_retry':retry_unknown})
            save(path,state)
        try:result=action()
        except Exception as exc:
            with LOCK:
                state=read(path)
                state.update(state='paused',error_type=type(exc).__name__,error=str(exc),updated_at=time.time())
                state['history'].append({'state':'paused','at':time.time(),'error_type':type(exc).__name__})
                save(path,state)
            raise
        with LOCK:
            state=read(path)
            state.update(state='completed',result=result,updated_at=time.time());state.pop('error',None)
            state['history'].append({'state':'completed','at':time.time()});save(path,state)
        return result
