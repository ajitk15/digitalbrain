"""Bake the draw.io MCP viewer's libavoid routes into portable editable XML.

Renderer and routing code are cached from the official draw.io hosts and MCP
resource. The saved geometry is library output, not manually placed waypoints.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
import urllib.request

from playwright.sync_api import sync_playwright

OUT=Path(__file__).parent
CACHE=OUT/".render-cache"


def ensure_cache():
    CACHE.mkdir(exist_ok=True)
    assets={
        "viewer-static.min.js":"https://app.diagrams.net/js/viewer-static.min.js",
        "libavoid.min.js":"https://viewer.diagrams.net/js/libavoid-js/libavoid.min.js",
        "libavoid-routing.js":"https://viewer.diagrams.net/js/libavoid-js/libavoid-routing.js",
    }
    for name,url in assets.items():
        if not (CACHE/name).exists():
            request=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(request,timeout=40) as response:
                (CACHE/name).write_bytes(response.read())
    if not (CACHE/"routing-glue.js").exists():
        from mcp_client import DrawioMCP
        resource=DrawioMCP().request("resources/read",{"uri":"ui://drawio/mcp-app.html"})
        source=resource["contents"][0]["text"]
        begin=source.index("function getAbsoluteParentOffset(")
        end=source.index("function edgesCrossShapes(")
        (CACHE/"routing-glue.js").write_text(source[begin:end],encoding="utf-8")


def main():
    ensure_cache()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel="chrome",headless=True)
        page=browser.new_page(viewport={"width":1300,"height":1200})
        page.set_content('<div id="graph" style="width:1200px;height:1100px"></div>')
        page.evaluate("window.urlParams={};window.mxBasePath='';window.mxLoadResources=false;window.mxLoadStylesheets=false;")
        for name in ["viewer-static.min.js","libavoid.min.js","libavoid-routing.js","routing-glue.js"]:
            page.add_script_tag(path=str(CACHE/name))
        print(page.evaluate("({graph:typeof Graph, avoid:typeof Avoid, core:typeof AvoidRouting})"),flush=True)
        page.evaluate("window.graph = new Graph(document.getElementById('graph'));graph.setHtmlLabels(true);graph.setEnabled(false);")
        reports=[]
        bundle=ET.Element("mxfile",host="app.diagrams.net",type="device")
        meta=json.loads((OUT/"pages.json").read_text(encoding="utf-8"))
        for p in meta:
            slug=p["slug"]
            xml=(OUT/f"{slug}.xml").read_text(encoding="utf-8")
            routed=page.evaluate("""async xml=>{
                const doc=mxUtils.parseXml(xml);
                graph.getModel().clear();
                new mxCodec(doc).decode(doc.documentElement,graph.getModel());
                graph.refresh();
                if(window.__libavoidReady) await window.__libavoidReady;
                const changed=routeWithLibavoid(graph,Avoid);
                const encoded=mxUtils.getXml(new mxCodec().encode(graph.getModel()));
                return {xml:encoded,changed};
            }""",xml)
            model=ET.fromstring(routed["xml"])
            model.set("adaptiveColors","auto")
            model.set("background","#ffffff")
            model.set("pageWidth","1120")
            model.set("pageHeight","1040")
            (OUT/f"{slug}.xml").write_text(ET.tostring(model,encoding="unicode"),encoding="utf-8")
            diagram=ET.SubElement(bundle,"diagram",id=slug,name=p["title"].split(" / ",1)[1])
            diagram.append(model)
            single=ET.Element("mxfile",host="app.diagrams.net",type="device")
            single.append(ET.fromstring(ET.tostring(diagram,encoding="unicode")))
            (OUT/f"{slug}.drawio").write_text(ET.tostring(single,encoding="unicode"),encoding="utf-8")
            reports.append({"page":slug,"libavoid_routed":routed["changed"]})
            print(slug,routed["changed"],flush=True)
        (OUT/"digital-brain-design.drawio").write_text(ET.tostring(bundle,encoding="unicode"),encoding="utf-8")
        (OUT/"routing-validation.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")
        browser.close()


if __name__=="__main__":
    main()
