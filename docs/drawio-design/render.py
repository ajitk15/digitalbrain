"""Render the actual editable XML with draw.io's documented embed/export protocol."""

import base64
import json
from pathlib import Path
from urllib.parse import unquote_to_bytes

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1200}, user_agent="Mozilla/5.0")
        page.set_content("""<!doctype html><html><body>
        <script>
        window.events=[];
        window.addEventListener('message', e=>{
          if(e.origin!=='https://embed.diagrams.net') return;
          try { window.events.push(JSON.parse(e.data)); } catch(_) {}
        });
        window.send = value => document.querySelector('iframe').contentWindow.postMessage(JSON.stringify(value),'https://embed.diagrams.net');
        </script>
        <iframe style="border:0;width:1350px;height:1150px" src="https://embed.diagrams.net/?embed=1&amp;proto=json&amp;spin=1&amp;ui=min&amp;libraries=0&amp;dark=0"></iframe>
        </body></html>""")
        page.wait_for_function("events.some(e=>e.event==='init')", timeout=60000)
        reports=[]
        for xml_path in sorted(OUT.glob("[0-9][0-9]-*.xml")):
            slug=xml_path.stem
            page.evaluate("events=[]")
            page.evaluate("xml=>send({action:'load',xml,fit:1,title:'Digital Brain',background:'#ffffff'})",xml_path.read_text(encoding="utf-8"))
            page.wait_for_function("events.some(e=>e.event==='load')",timeout=30000)
            page.evaluate("send({action:'export',format:'xmlsvg',background:'#ffffff',border:20})")
            page.wait_for_function("events.some(e=>e.event==='export')",timeout=30000)
            event=page.evaluate("events.find(e=>e.event==='export')")
            header, payload=event["data"].split(",",1)
            svg=base64.b64decode(payload) if ";base64" in header else unquote_to_bytes(payload)
            (OUT/f"{slug}.svg").write_bytes(svg)
            # Screenshot the SVG using a browser so HTML foreignObject labels render correctly.
            imagepage=browser.new_page(viewport={"width":1180,"height":1140},device_scale_factor=1.5)
            imagepage.goto((OUT/f"{slug}.svg").resolve().as_uri())
            imagepage.locator("svg").screenshot(path=str(OUT/f"{slug}.png"))
            imagepage.close()
            reports.append({"page":slug,"native_drawio_import":True,"svg_export":True,"png_preview":True})
            print(f"Rendered {slug}",flush=True)
        browser.close()
        (OUT/"render-validation.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")


if __name__=="__main__":
    main()
