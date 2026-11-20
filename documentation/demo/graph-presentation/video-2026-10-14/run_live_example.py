"""Execute real project functions on synthetic sources, with controlled proposals."""

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'digitalbrain.settings')
import django
django.setup()

from platform_core.graphs import build_graph
from platform_core.graph_ai import enrich_graph, judge
from platform_core.graph_quality import themes

app = '00000000-0000-0000-0000-000000000101'
sources = [
    SimpleNamespace(pk='00000000-0000-0000-0000-000000000201', application_id=app,
        title='Library note', content='## Book list\n\nThe library app uses a book list.'),
    SimpleNamespace(pk='00000000-0000-0000-0000-000000000202', application_id=app,
        title='Help guide', content='## Help\n\nThe help guide explains the library app.'),
]
for source in sources:
    source.digest = hashlib.sha256(source.content.encode()).hexdigest()
proposals = {
    sources[0].pk: [dict(subject='library app', subject_type='Component', relation='uses',
        object='book list', object_type='Component', source_id=sources[0].pk,
        quote='The library app uses a book list.')],
    sources[1].pk: [dict(subject='Help guide', subject_type='Runbook', relation='explains',
        object='library app', object_type='Component', source_id=sources[1].pk,
        quote='The help guide explains the library app.')],
}
data, quality = build_graph(sources, 2)
structural = copy.deepcopy(data)
assert not any(edge.get('inferred') for edge in structural['edges'])
config = SimpleNamespace(configured_by_id='00000000-0000-0000-0000-000000000301',
    provider='openai', model='controlled-teaching-example')
with patch('platform_core.graph_ai.reusable', return_value=([], {}, {})), patch(
    'platform_core.graph_ai.extract_relationships', side_effect=lambda a, c, s: proposals[s['id']]):
    data, quality = enrich_graph(app, config, sources, data, quality)
bad = {**proposals[sources[0].pk][0], 'quote':'The library app uses a toy list.'}
accepted, reason = judge(bad, {sources[0].pk:dict(title=sources[0].title,
                                               excerpt=sources[0].content)})
assert accepted is None and reason == 'quote not in source'
semantic = [edge for edge in data['edges'] if edge.get('inferred')]
entities = [node for node in data['nodes'] if node['kind'] == 'entity']
assert len(semantic) == 2
assert sum(node['label'].casefold() == 'library app' for node in entities) == 1
clusters = themes(data['nodes'], data['edges'])
record = dict(sources=[vars(source) for source in sources], proposals=proposals,
    structural=structural, enriched=data, quality=quality, themes=clusters,
    verification=dict(structural_builder='passed', enrichment_and_merging='passed',
        semantic_links=len(semantic), entity_nodes=len(entities),
        shared_library_app_nodes=1, invented_quote_rejected=True, rejection_reason=reason,
        graphify_themes='executed', ai_provider='controlled proposals; no live call',
        persistence_and_publication='explained; not performed',
        production_source_records='not loaded or modified'))
Path(__file__).with_name('live-example.json').write_text(json.dumps(record, indent=2),encoding='utf-8')
print(json.dumps(record['verification'], indent=2))
