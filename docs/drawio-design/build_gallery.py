"""Package the validated pages as an offline browsing gallery."""

import html
import json
import zipfile
from pathlib import Path

OUT=Path(__file__).parent
pages=json.loads((OUT/"pages.json").read_text(encoding="utf-8"))
checks=json.loads((OUT/"validation.json").read_text(encoding="utf-8"))
cards=[]
buttons=[]
for i,(page,check) in enumerate(zip(pages,checks,strict=True)):
    slug=page["slug"]
    title=html.escape(page["title"])
    url=html.escape(check["editor_url"],quote=True)
    buttons.append(f'<button data-page="{i}" onclick="selectPage({i})">{title}</button>')
    svg=(OUT/f"{slug}.svg").read_text(encoding="utf-8")
    svg=svg[svg.index("<svg"):]
    cards.append(f'''<section data-page="{i}" {'hidden' if i else ''}>
    <div class="heading"><div><p class="eyebrow">DIGITAL BRAIN / SYSTEM DESIGN</p><h1>{title}</h1>
    <p>{html.escape(page['subtitle'])}</p></div></div>
    <div class="actions"><a href="{url}" target="_blank" rel="noopener">Edit in draw.io ↗</a>
    <a href="{slug}.drawio" download>Download .drawio</a>
    <a href="{slug}.png" download>PNG</a><a href="{slug}.svg" download>SVG</a>
    <button onclick="zoom(0.2)" aria-label="Zoom in">＋</button><button onclick="zoom(-0.2)" aria-label="Zoom out">−</button>
    <button onclick="setZoom(1)">Fit</button></div>
    <div class="canvas" tabindex="0">{svg}</div>
    <details><summary>Evidence and scope</summary><p>{html.escape(page['evidence'])}</p>
    <p>Based on the repository working tree at 08862f2, 19 October 2026. Hostinger describes committed configuration; live infrastructure was not inspected. AWS is a proposed single-host adaptation, not an applied deployment.</p></details>
    </section>''')

document='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Digital Brain — architecture and process diagrams</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#edf1f6;color:#22314b;font:15px/1.55 system-ui,sans-serif}
aside{position:fixed;inset:0 auto 0 0;width:285px;background:#102039;color:#e9f1ff;padding:28px 20px;overflow:auto}
.brand{font-weight:760;font-size:24px;letter-spacing:-.6px}.tag{font-size:12px;color:#9eb4d1;margin:6px 0 27px}
nav{display:grid;gap:8px}button,a{font:inherit}nav button{border:1px solid transparent;background:transparent;color:#cbd7e8;padding:13px 12px;border-radius:9px;text-align:left;cursor:pointer;line-height:1.4}
nav button.active{background:#244367;color:white;border-color:#466589}nav button:hover{background:#1c3554}
.bundle{display:block;margin-top:24px;color:#c8e3ff;text-decoration:none;border:1px solid #466589;padding:12px;border-radius:8px}
.aside-note{font-size:12px;color:#9eb4d1;margin-top:20px}main{margin-left:285px;padding:32px;max-width:1800px}
h1{font-size:28px;line-height:1.2;letter-spacing:-.7px;margin:7px 0 10px}.eyebrow{font-size:11px;letter-spacing:1.5px;font-weight:750;color:#607591;margin:0}
.heading p:not(.eyebrow){color:#5d6c82;margin:8px 0 20px}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:20px}
.actions a,.actions button{background:white;color:#294567;border:1px solid #c5d0df;text-decoration:none;padding:8px 13px;border-radius:7px;cursor:pointer}
.actions a:first-child{background:#204c79;color:white;border-color:#204c79}.canvas{overflow:auto;border:1px solid #d0d8e4;border-radius:12px;background:white;box-shadow:0 8px 30px #1429440a}
.canvas svg{display:block;width:100%;height:auto;min-width:650px}details{margin-top:17px;color:#60708a;font-size:13px}summary{cursor:pointer}section[hidden]{display:none}
button:focus-visible,a:focus-visible,.canvas:focus-visible{outline:3px solid #44a7ff;outline-offset:3px}
@media(max-width:850px){aside{position:static;width:100%}nav{grid-template-columns:repeat(2,minmax(0,1fr))}main{margin-left:0;padding:20px}.brand{font-size:22px}.tag{margin-bottom:12px}h1{font-size:24px}}
@media print{aside,.actions,details{display:none}main{margin:0;padding:0}.canvas{border:0;box-shadow:none}.canvas svg{min-width:0}}
</style><aside><div class="brand">Digital Brain</div><p class="tag">ARCHITECTURE & PROCESS ATLAS · 9 DIAGRAMS</p><nav>BUTTONS</nav>
<a class="bundle" href="digital-brain-design.drawio" download>Download all 9 pages (.drawio)</a>
<p class="aside-note">Created through draw.io MCP.<br>Blue = processing · Green = data<br>Purple = security · Yellow = gate<br>Orange = external · Red = failure</p></aside><main>CARDS</main>
<script>
let current=0,level=1;
function selectPage(n){current=n;document.querySelectorAll('section').forEach((s,i)=>s.hidden=i!==n);document.querySelectorAll('nav button').forEach((b,i)=>{b.classList.toggle('active',i===n);b.setAttribute('aria-current',i===n?'page':'false')});setZoom(1);history.replaceState(null,'','#'+(n+1))}
function setZoom(n){level=Math.max(.6,Math.min(3,n));const svg=document.querySelector('section:not([hidden]) svg');svg.style.width=(level*100)+'%';svg.style.maxWidth='none'}
function zoom(delta){setZoom(level+delta)}
selectPage(Math.max(0,Math.min(8,(parseInt(location.hash.slice(1))||1)-1)));
</script></html>'''.replace('BUTTONS','\n'.join(buttons)).replace('CARDS','\n'.join(cards))
(OUT/"index.html").write_text(document,encoding="utf-8")
with zipfile.ZipFile(OUT/"digital-brain-design.zip","w",zipfile.ZIP_DEFLATED) as archive:
    for path in OUT.iterdir():
        if path.suffix in {".drawio",".svg",".png",".html"} or path.name=="README.md":
            archive.write(path,path.name)
print("Gallery and ZIP created")
