import zipfile,json,xml.etree.ElementTree as E,pathlib
z=zipfile.ZipFile('C:/Users/ajitk/Downloads/Digital-Brain-Leadership-Slide-v3.pptx'); ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main','p':'http://schemas.openxmlformats.org/presentationml/2006/main'}
r=E.fromstring(z.read('ppt/slides/slide1.xml'))
texts=['\n'.join(''.join(t.text or '' for t in p.findall('.//a:t',ns)) for p in s.findall('.//a:p',ns)) for s in r.findall('.//p:sp',ns) if s.findall('.//a:t',ns)]
pathlib.Path('.slide-redesign/source-text.json').write_text(json.dumps(texts,ensure_ascii=False),encoding='utf8')
for f in z.namelist():
 if f.startswith('ppt/media/'): pathlib.Path('.slide-redesign/'+f.split('/')[-1]).write_bytes(z.read(f))
print(json.dumps(texts[-16:],ensure_ascii=False,indent=2))
