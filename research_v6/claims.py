"""Original passages, separate claim verification, and bounded public retrieval."""
import hashlib
import re
import time
import threading
LOCK=threading.RLock()
NETWORK_LOCK=threading.Lock()
LAST_FETCH=0.0
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from tournament import save, read

class EvidenceStore:
    def __init__(self,path):
        self.path=Path(path);self.data=read(path) if self.path.exists() else {'version':1,'sources':{},'claims':{},'requests':{}}
    def flush(self):
        with LOCK:save(self.path,self.data)
    def ingest(self,sources):
        for source in sources:
            item=dict(source);item['access_status']='retrieved_passage';item['ingested_at']=item.get('ingested_at',time.time())
            prior=self.data['sources'].get(item['id'])
            if prior and prior['text']!=item['text']:raise ValueError('Source changed: '+item['id'])
            self.data['sources'][item['id']]=item
        self.flush()
    def add_claim(self,claim,source_id,passage,**scope):
        source=self.data['sources'].get(source_id)
        verified=bool(source and passage and passage in source['text'])
        item={'claim':claim,'source_id':source_id,'location':source.get('location','') if source else '',
              'exact_passage':passage,'passage_verification':'verified' if verified else 'unverified',
              'claim_support':'not_assessed','species':scope.get('species'),
              'perturbation':scope.get('perturbation'),'developmental_stage':scope.get('developmental_stage'),
              'tissue':scope.get('tissue'),'endpoint':scope.get('endpoint'),'scope':scope}
        key=hashlib.sha256((claim+source_id+passage).encode()).hexdigest()[:20]
        with LOCK:
            self.data['claims'][key]=item;self.flush()
        return item
    def retrieve(self,request_id,query,limit=2,fulltext=True,fetch=None):
        if request_id in self.data['requests']:return self.data['requests'][request_id]
        fetch=fetch or self._fetch
        record={'query':query,'status':'retrieving','sources':[],'errors':[]}
        self.data['requests'][request_id]=record;self.flush()
        try:
            search='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','term':query,'retmax':limit,'retmode':'xml'})
            ids=[x.text for x in ET.fromstring(fetch(search)).findall('./IdList/Id')]
            if ids:
                url='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','id':','.join(ids),'retmode':'xml'})
                root=ET.fromstring(fetch(url));sources=[]
                for article in root.findall('.//PubmedArticle'):
                    pmid=article.findtext('.//MedlineCitation/PMID');title=''.join(article.find('.//ArticleTitle').itertext()) if article.find('.//ArticleTitle') is not None else ''
                    abstract='\n'.join(''.join(p.itertext()) for p in article.findall('.//Abstract/AbstractText'))
                    if abstract:sources.append({'id':'PUBMED-'+pmid+'-ABSTRACT','paper_id':'PMID:'+pmid,'title':title,'url':'https://pubmed.ncbi.nlm.nih.gov/'+pmid+'/', 'location':'PubMed abstract','access':'abstract_only','text':abstract})
                    if fulltext:
                        try:
                            lookup='https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urllib.parse.urlencode({'query':'EXT_ID:'+pmid+' AND SRC:MED','format':'json'})
                            import json
                            matches=json.loads(fetch(lookup)).get('resultList',{}).get('result',[])
                            pmcid=next((x.get('pmcid') for x in matches if x.get('pmcid')),None)
                            if pmcid:
                                fullurl='https://www.ebi.ac.uk/europepmc/webservices/rest/'+pmcid+'/fullTextXML'
                                xml=ET.fromstring(fetch(fullurl))
                                passages=xml.findall('.//body//p')+xml.findall('.//fig/caption')
                                for index,p in enumerate(passages):
                                    text=''.join(p.itertext()).strip()
                                    if text:sources.append({'id':pmcid+'-PASSAGE-'+str(index),'paper_id':'PMID:'+pmid,'title':title,'url':'https://pmc.ncbi.nlm.nih.gov/articles/'+pmcid+'/', 'location':'XML paragraph/caption '+str(index),'access':'fulltext_passage','text':text})
                            else:record['errors'].append('No accessible PMC full text located for PMID '+pmid)
                        except Exception as exc:record['errors'].append('Full text unavailable for PMID '+pmid+': '+type(exc).__name__)
                self.ingest(sources);record['sources']=[s['id'] for s in sources]
            record['status']='retrieved' if record['sources'] else 'no_accessible_passages'
        except Exception as exc:record.update(status='retrieval_failed');record['errors'].append(type(exc).__name__+': '+str(exc)[:200])
        self.flush();return record
    @staticmethod
    def _fetch(url):
        global LAST_FETCH
        with NETWORK_LOCK:
            delay=0.35-(time.monotonic()-LAST_FETCH)
            if delay>0:time.sleep(delay)
            LAST_FETCH=time.monotonic()
        request=urllib.request.Request(url,headers={'User-Agent':'ECMResearchTournament/6 (public literature retrieval)'})
        with urllib.request.urlopen(request,timeout=20) as response:return response.read()
