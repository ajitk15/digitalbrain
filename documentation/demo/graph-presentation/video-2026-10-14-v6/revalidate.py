import hashlib,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','digitalbrain.settings')
import django
django.setup()
from platform_core.graphs import build_graph
from platform_core.graph_ai import enrich_graph,judge
from platform_core.graph_quality import themes
raw=json.loads((HERE/'source-content.json').read_text(encoding='utf-8'))
app='00000000-0000-0000-0000-000000000101'
quotes=[next(line for line in r['content'].splitlines() if token in line) for r,token in zip(raw,['### US-08','| `domain/referral_state.py`','| FR-08 |'])]
items=[('US-08','Ticket','references','FR-08','Requirement'),('FR-08','Requirement','implemented in','domain/referral_state.py','Code'),('TestLifecycleMap','Code','verifies','FR-08','Requirement')]
sources=[];proposals={}
for k,(r,q,it) in enumerate(zip(raw,quotes,items)):
 src=SimpleNamespace(pk=f'00000000-0000-0000-0000-{301+k:012d}',application_id=app,title=r['title'],content=r['content'])
 src.digest=hashlib.sha256(src.content.encode()).hexdigest();sources.append(src)
 a,at,rel,z,zt=it
 proposals[src.pk]=[dict(subject=a,subject_type=at,relation=rel,object=z,object_type=zt,source_id=src.pk,quote=q)]
 accepted,reason=judge(proposals[src.pk][0],{src.pk:dict(title=src.title,excerpt=src.content)})
 assert accepted is not None,(k,reason)
base,quality=build_graph(sources,len(sources))
structural=dict(nodes=len(base['nodes']),edges=len(base['edges']))
config=SimpleNamespace(configured_by_id=app,provider='openai',model='prepared-teaching-proposal')
with patch('platform_core.graph_ai.reusable',return_value=([],{},{})),patch('platform_core.graph_ai.extract_relationships',side_effect=lambda a,c,s:proposals[s['id']]):
 graph,quality=enrich_graph(app,config,sources,base,quality)
entities=[n for n in graph['nodes'] if n['kind']=='entity']
assert len(entities)==4,len(entities)
assert len([n for n in entities if n['label']=='FR-08'])==1
record=dict(structural=structural,business_nodes=len(entities),business_edges=len([e for e in graph['edges'] if e.get('inferred')]),quotes=quotes,proposals=proposals,graph=graph,themes=themes(graph['nodes'],graph['edges']),scope='Real local CarePath documents converted with MarkItDown. Actual graph builder and validator with prepared AI proposals. No live model, embedding or publish call. Video shows a focused graph subset.')
(HERE/'carepath-revalidation.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in record.items() if k in ['structural','business_nodes','business_edges','scope']},indent=2))
