from pathlib import Path
import zipfile,importlib.util,json,collections,types,gc
import UnityPy
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.streams import EndianBinaryReader

try:
    from patcher import bundle_crypto as c
except ModuleNotFoundError as error:
    if error.name != 'patcher':
        raise
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('compress',root/'Tools/Compress-VitaBundles.py')
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
old=ObjectReader.parse_as_object

def load(raw):
    plain = raw if raw.startswith(b'UnityFS') else c.decrypt(raw)
    parse = ObjectReader.parse_as_object
    try:
        ObjectReader.parse_as_object = lambda self, *a, **k: (
            types.SimpleNamespace(m_Container=[], m_PreloadTable=[], m_Name='InspectionOnly')
            if self.type.name == 'AssetBundle' else parse(self, *a, **k))
        return UnityPy.load(plain)
    finally:
        ObjectReader.parse_as_object = parse

def read_object(o):
    raw=o.get_raw_data();reader,start=o.reader,o.byte_start;fail=[]
    try:
        for shift in range(4):
            n=int.from_bytes(raw[shift:shift+4],'little')
            if n<1 or n>512:continue
            name=raw[shift+4:shift+4+n]
            if any(x<32 or x>126 for x in name):continue
            o.reader=EndianBinaryReader(raw[shift:]+b'\0'*shift,endian=reader.endian);o.byte_start=0
            try:
                d=o.read_typetree(check_read=False)
                assert d['m_Name']==name.decode()
                return d,shift
            except Exception as ex:fail.append(str(ex))
        raise ValueError('Cannot safely parse '+o.type.name+': '+'; '.join(fail))
    finally:o.reader,o.byte_start=reader,start

def info(env):
    textures={};sprites={};shifts=[]
    for o in env.objects:
        if o.type.name not in ('Sprite','Texture2D'):continue
        d,shift=read_object(o)
        if shift:shifts.append([o.type.name,d['m_Name'],shift])
        if o.type.name=='Texture2D':textures[d['m_Name']]=[d['m_Width'],d['m_Height'],d['m_TextureFormat'],d['m_MipCount']]
        else:sprites[d['m_Name']]=d
    return {'textures':textures,'sprites':sprites,'shifts':shifts,'types':dict(collections.Counter(o.type.name for o in env.objects)),'platforms':sorted(set(int(a.target_platform) for a in env.assets))}
