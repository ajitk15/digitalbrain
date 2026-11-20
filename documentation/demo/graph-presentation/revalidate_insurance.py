"""Read-only synthetic demo: real graph functions, prepared AI proposals."""
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'digitalbrain.settings')
import django
django.setup()
from platform_core.graphs import build_graph
from platform_core.graph_ai import enrich_graph, judge, prepare, walk
from platform_core.graph_quality import themes
from platform_core.embeddings import fuse

app = '00000000-0000-0000-0000-000000000101'
examples = [
    ('Policy System.pdf', 'The claims portal uses the policy database to check policy details.', 'claims portal', 'Component', 'uses', 'policy database', 'Component'),
    ('Claims Processing.docx', 'The claims portal uses the claims database to store claim records.', 'claims portal', 'Component', 'uses', 'claims database', 'Component'),
    ('Claims Guide.docx', 'The claims guide explains the claims portal.', 'claims guide', 'Runbook', 'explains', 'claims portal', 'Component'),
]
sources, proposals = [], {}
for i, (title, quote, subject, stype, relation, obj, otype) in enumerate(examples, 201):
    source = SimpleNamespace(pk=f'00000000-0000-0000-0000-{i:012d}', application_id=app, title=title, content=f'## Overview\n\n{quote}')
    source.digest = hashlib.sha256(source.content.encode()).hexdigest()
    sources.append(source)
    proposals[source.pk] = [dict(subject=subject, subject_type=stype, relation=relation, object=obj, object_type=otype, source_id=source.pk, quote=quote)]
data, quality = build_graph(sources, len(sources))
structural_counts = dict(nodes=len(data['nodes']), edges=len(data['edges']))
config = SimpleNamespace(configured_by_id=app, provider='openai', model='prepared-demo')
with patch('platform_core.graph_ai.reusable', return_value=([], {}, {})), patch('platform_core.graph_ai.extract_relationships', side_effect=lambda a, c, s: proposals[s['id']]):
    data, quality = enrich_graph(app, config, sources, data, quality)
entities = [n for n in data['nodes'] if n['kind'] == 'entity']
edges = [e for e in data['edges'] if e.get('inferred')]
assert len(entities) == 4 and len(edges) == 3
assert sum(n['label'].casefold() == 'claims portal' for n in entities) == 1
bad = dict(proposals[sources[0].pk][0], object='payment database', quote='The claims portal uses a payment database.')
accepted, reason = judge(bad, {sources[0].pk: dict(title=sources[0].title, excerpt=sources[0].content)})
assert accepted is None and reason == 'quote not in source'
ready = prepare(data)
seed = next(row for row in ready.candidates if row[0] is edges[0])
paths = walk(data, [seed])
assert any(e['target'] == edges[1]['target'] for e, *_ in paths)
assert fuse([0, 1], [1, 0])[0] == 0
record = dict(entity_nodes=len(entities), business_edges=len(edges), total_nodes=len(data['nodes']), total_edges=len(data['edges']), structural_counts=structural_counts, invented_quote_rejected=reason, graph_walk_reached_related_database=True, graphify_themes=themes(data['nodes'], data['edges']), sources=[vars(s) for s in sources], graph=data, scope='Normalized synthetic text; prepared AI proposals. No upload conversion, provider request, embedding request, persistence, publication, or end-to-end answer executed.')
Path(__file__).with_name('insurance-revalidation.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in record.items() if k not in {'graph', 'sources', 'graphify_themes'}}, indent=2))
