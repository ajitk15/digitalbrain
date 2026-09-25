import zipfile, xml.etree.ElementTree as E
p='C:/Users/ajitk/Downloads/Digital-Brain-Leadership-Slide-v3.pptx'
z=zipfile.ZipFile(p)
ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main','p':'http://schemas.openxmlformats.org/presentationml/2006/main'}
print(z.read('ppt/presentation.xml').decode())
for f in z.namelist():
 if f.startswith('ppt/slides/slide') and f.endswith('.xml'):
  print('\nFILE',f)
  r=E.fromstring(z.read(f))
  for sp in r.findall('.//p:sp',ns):
   texts=sp.findall('.//a:t',ns)
   if texts: print(repr(' | '.join(t.text or '' for t in texts)))
print('MEDIA', [f for f in z.namelist() if f.startswith('ppt/media/')])
