import zipfile,xml.etree.ElementTree as E,re,collections
ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
def words(path):
 z=zipfile.ZipFile(path);r=E.fromstring(z.read('ppt/slides/slide1.xml')); t=' '.join(x.text or '' for x in r.findall('.//a:t',ns));return collections.Counter(re.findall(r'[\w]+(?:[-&][\w]+)*',t))
a=words('C:/Users/ajitk/Downloads/Digital-Brain-Leadership-Slide-v3.pptx');b=words('outputs/leadership-slide/Digital-Brain-Leadership-Redesigned-v4.pptx')
print('Missing words:',dict(a-b)); print('Added words:',dict(b-a))
