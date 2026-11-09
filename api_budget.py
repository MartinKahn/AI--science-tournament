"""Persistent conservative reservations for a USD-capped API run.

Never rely on cache discounts. Unknown/failed requests keep their reservation.
Billing guard is conditional on the pinned tariff and text-only request contract.
"""
import json
from pathlib import Path
import threading
import time

LOCK=threading.Lock()
class BudgetExceeded(RuntimeError):pass

def write(path,data):
    path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2)+'\n');tmp.replace(path)

def input_token_bound(payload):
    # For byte-level tokenizers one UTF-8 byte per token is a conservative
    # content bound; reserve additional message/schema framing overhead.
    content={'input':payload['input'],'text':payload['text']}
    return len(json.dumps(content,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+4096

def reservation_usd(payload,pricing):
    return (input_token_bound(payload)*pricing['input_per_million']*pricing.get('input_safety_multiplier',1.25)
            +payload['max_output_tokens']*pricing['output_per_million'])/1_000_000

def usage_upper_cost(usage,pricing):
    # Reserve all input at the cache-write premium, all output (including
    # reasoning) at output rate. Never subtract cached-token discounts.
    if not isinstance(usage,dict) or 'input_tokens' not in usage or 'output_tokens' not in usage:return None
    return (usage['input_tokens']*pricing['input_per_million']*pricing.get('input_safety_multiplier',1.25)
            +usage['output_tokens']*pricing['output_per_million'])/1_000_000

class Budget:
    def __init__(self,run_dir,config):
        self.path=Path(run_dir)/'budget.json';self.config=config
        if not 0<config['budget_usd']<=10:raise ValueError('Run cap must be positive and no greater than the user ceiling of $10')
    def reserve(self,job_id,payload,pricing=None):
        pricing=pricing or self.config['pricing']
        amount=reservation_usd(payload,pricing)
        with LOCK:
            state=json.loads(self.path.read_text()) if self.path.exists() else {'cap_usd':self.config['budget_usd'],'entries':[]}
            if state['cap_usd']!=self.config['budget_usd']:raise ValueError('Budget cap changed; use a new run')
            committed=sum(x.get('accounted_usd',x['reserved_usd']) for x in state['entries'])
            if committed+amount>state['cap_usd']:
                raise BudgetExceeded(f'Budget pause: ${committed:.3f} accounted/reserved; next request reserves ${amount:.3f}; cap ${state["cap_usd"]:.2f}')
            token=f'{job_id}-{time.time_ns()}'
            state['entries'].append({'reservation_id':token,'job_id':job_id,'reserved_usd':amount,'pricing':pricing,'model':payload.get('model',pricing.get('model')),'status':'pending_or_unknown'})
            write(self.path,state)
            return token
    def settle(self,token,usage):
        with LOCK:
            state=json.loads(self.path.read_text());entry=next(x for x in state['entries'] if x['reservation_id']==token)
            actual=usage_upper_cost(usage,entry.get('pricing',self.config['pricing']))
            if actual is not None:
                if actual>entry['reserved_usd']:
                    # Account the excess and halt rather than continue with an
                    # invalid reservation assumption.
                    entry.update(accounted_usd=actual,status='reservation_bound_exceeded',usage=usage);write(self.path,state)
                    raise RuntimeError('Provider usage exceeded the conservative reservation; halt and review tariff/token assumptions')
                entry.update(accounted_usd=actual,status='settled_conservative_usage',usage=usage)
                write(self.path,state)
