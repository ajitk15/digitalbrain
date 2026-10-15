import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
draft = (HERE.parent/'graph-generation-4-minute-storyboard.md').read_text(encoding='utf-8')
chapters = re.split(r'\n## \d+ · ', draft)[1:]
durations = [22,25,39,44,26,22,24,16,22]
titles = ['Upload source documents','Convert files into readable text','Build the document structure','Extract business relationships','Check the supporting evidence','Join matching business entities','Prepare search embeddings','Save and organise the draft','Review and publish']
tech = ['Django · Python · private file storage','MarkItDown · Django KnowledgeEntry · SHA-256','Custom Python structural graph builder','Optional OpenAI / Claude · Python enrichment','Python validator · project ontology','Python entity merging · source provenance','Optional OpenAI embeddings · Django storage','Django GraphRevision · Graphify · NetworkX · Leiden','Django publication workflow · access and evidence checks']
scenes = []
for i, chapter in enumerate(chapters[:9]):
    narration = re.search(r'\*\*Narration\*\*\s+“(.+?)”', chapter, re.S).group(1)
    scenes.append(dict(title=titles[i], duration=durations[i], narration=narration, tech=tech[i]))
assert len(scenes)==9 and sum(s['duration'] for s in scenes)==240
(HERE/'storyboard.json').write_text(json.dumps(scenes, indent=2, ensure_ascii=False), encoding='utf-8')
offset=0
transcript=['# Digital Brain graph generation', 'Indian English narration — Microsoft Heera (en-IN).']
for scene in scenes:
    transcript.extend([f'## {offset//60:02d}:{offset%60:02d} — {scene["title"]}',scene['narration'],'Technology: '+scene['tech']])
    offset+=scene['duration']
(HERE/'transcript.md').write_text('\n\n'.join(transcript), encoding='utf-8')
print('Prepared nine scenes / 240 seconds.')
