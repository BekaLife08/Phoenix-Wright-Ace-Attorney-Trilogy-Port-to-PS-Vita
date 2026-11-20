"""Stage a Russian overlay using actual PSP2 bundles as serialization templates.

No base game, Unity Assets directory, PC installation or existing build is edited.
PC textures and translation payloads are inputs; native shaders, model data,
ScriptableObject schemas and atlas limits come from the port's native bundles.
"""
from pathlib import Path
import argparse,copy,gc,hashlib,json,struct,time,zipfile
from PIL import Image
from UnityPy.enums import TextureFormat
from UnityPy.streams import EndianBinaryReader
import bundle_reader as reader

PREFIX='Text/Steam/PWAAT_Data/StreamingAssets/'
SKIP={'menu/common/court_record_03_pcu.unity3d','menu/common/court_record_02_ps4u.unity3d'}
NEW={'GS3/BG/spr40402.unity3d','GS3/BG/spr40403.unity3d','GS3/BG/spr40404.unity3d'}

def native_objects(env,kind,duplicates=False):
    result={}
    for obj in env.objects:
        if obj.type.name!=kind:continue
        tree=reader.read_object(obj)[0] if kind in ('Sprite','Texture2D','AssetBundle') else obj.read_typetree()
        key=tree['m_Name'].casefold()
        if key in result:
            if not duplicates:raise ValueError('Ambiguous object '+kind+' '+key)
            key+=' #'+str(obj.path_id)
        result[key]=(obj,tree)
    return result

def parsed_texture(obj):
    # The translator's editor leaves some object records at an unaligned start.
    # Use the validated reader and then deserialize from a bounded local buffer.
    _,shift=reader.read_object(obj)
    raw=obj.get_raw_data();old,start=obj.reader,obj.byte_start
    obj.reader=EndianBinaryReader(raw[shift:]+b'\0'*shift,endian=old.endian);obj.byte_start=0
    try:return obj.read(check_read=False)
    finally:obj.reader,obj.byte_start=old,start

def prepare_sprite(source,original,native,source_texture,native_texture,path_map):
    result=copy.deepcopy(native)
    # Object shape is serialized through the native Sprite type tree.
    for key in result:
        if key in source and key not in ('m_Name','m_RenderDataKey','m_SpriteAtlas','m_AtlasTags'):
            result[key]=copy.deepcopy(source[key])
    rect=source['m_Rect']
    sw,sh=source_texture['m_Width'],source_texture['m_Height']
    if rect['x']<0 or rect['y']<0 or rect['x']+rect['width']>sw+.01 or rect['y']+rect['height']>sh+.01:
        # label0u in the published PC patch has x=163,width=512 on a 512px
        # texture. Its original logical rect is valid and preserves its anchor.
        rect=original['m_Rect'] if original else dict(x=0.,y=0.,width=float(sw),height=float(sh))
    scale=native['m_PixelsToUnits']/source['m_PixelsToUnits']
    result['m_Rect']={key:value*scale for key,value in rect.items()}
    result['m_PixelsToUnits']=native['m_PixelsToUnits']
    for field in ('m_Border','m_Offset'):
        if field in result:result[field]={key:value*scale for key,value in source[field].items()}
    rd=result['m_RD']
    for field in ('texture','alphaTexture'):
        pointer=rd.get(field)
        if pointer and pointer['m_PathID']:
            if pointer['m_FileID']!=0 or pointer['m_PathID'] not in path_map:raise ValueError('Unmapped Sprite texture')
            pointer['m_PathID']=path_map[pointer['m_PathID']]
    # Expand tight English crops to the original logical rect. Runtime full-rect
    # geometry then covers the translated alpha silhouette without clipping.
    rd['textureRect']=dict(result['m_Rect'])
    rd['textureRectOffset']={'x':0.,'y':0.}
    if 'uvTransform' in rd:rd['uvTransform']={key:value*scale for key,value in source['m_RD']['uvTransform'].items()}
    result['m_SpriteAtlas']={'m_FileID':0,'m_PathID':0}
    result['m_AtlasTags']=[]
    return result

def convert_bundle(rel,raw,pc_root,native_root,animation_fields=None):
    ru=reader.load(raw)
    ru_tex=native_objects(ru,'Texture2D');ru_sprites=native_objects(ru,'Sprite')
    is_new=rel in NEW
    original=None if is_new or pc_root is None else reader.load((pc_root/rel).read_bytes())
    orig_sprites={} if original is None else native_objects(original,'Sprite')
    if is_new:
        base=reader.load((native_root/'GS1/BG/bg00bu.unity3d').read_bytes())
    else:base=reader.load((native_root/rel).read_bytes())
    if {int(x.target_platform) for x in base.assets}!={30}:raise ValueError('Template must be real PSP2 data')
    if len(base.assets)!=1:raise ValueError('Multi-file template needs explicit dependency mapping')
    native_tex=native_objects(base,'Texture2D',True);native_sprites=native_objects(base,'Sprite',True)
    path_map={};tex_names={};converted_ids=set();textures=[]
    if is_new and (len(ru_tex)!=1 or len(ru_sprites)!=1 or len(native_tex)!=1 or len(native_sprites)!=1):raise ValueError('New Sprite template must be one Texture and Sprite')
    for key,(source_obj,source_tree) in ru_tex.items():
        targets=list(native_tex.values()) if is_new else [v for k,v in native_tex.items() if k.split(' #')[0]==key]
        if not targets:raise ValueError('Missing native Texture '+key)
        source=parsed_texture(source_obj);source_image=source.image
        if source_image is None:raise ValueError('Missing translated pixels '+key)
        for target_obj,target_tree in targets:
            width,height=(source_tree['m_Width'],source_tree['m_Height']) if is_new else (target_tree['m_Width'],target_tree['m_Height'])
            if is_new and max(width,height)>2048:raise ValueError('New Sprite exceeds the Sprite atlas limit')
            image=source_image if source_image.size==(width,height) else source_image.resize((width,height),Image.Resampling.LANCZOS)
            texture=target_obj.read();fmt=target_tree['m_TextureFormat']
            if fmt in (28,29):fmt=12
            if fmt==10 and image.mode=='RGBA' and image.getchannel('A').getextrema()[0]<250:fmt=12
            if is_new:fmt=12;texture.m_Name=source_tree['m_Name']
            texture.set_image(image,target_format=fmt,mipmap_count=1);texture.save()
            path_map[source_obj.path_id]=target_obj.path_id;tex_names[(source_obj.path_id,target_obj.path_id)]=(source_tree,dict(target_tree,m_Width=width,m_Height=height))
            converted_ids.add(target_obj.path_id);textures.append({'name':texture.m_Name,'width':width,'height':height,'format':int(texture.m_TextureFormat)})
    sprite_count=0
    for key,(source_obj,source) in ru_sprites.items():
        targets=list(native_sprites.values()) if is_new else [v for k,v in native_sprites.items() if k.split(' #')[0]==key]
        if not targets:raise ValueError('Missing native Sprite '+key)
        for target_obj,native in targets:
            if is_new:native=copy.deepcopy(native);native['m_PixelsToUnits']=source['m_PixelsToUnits']
            original_tree=orig_sprites.get(key,(None,None))[1]
            source_texture_id=source['m_RD']['texture']['m_PathID']
            target_texture_id=path_map[source_texture_id] if is_new else native['m_RD']['texture']['m_PathID']
            source_tex,target_tex=tex_names[(source_texture_id,target_texture_id)]
            sprite_map=dict(path_map);sprite_map[source_texture_id]=target_texture_id
            tree=prepare_sprite(source,original_tree,native,source_tex,target_tex,sprite_map)
            if is_new:tree['m_Name']=source['m_Name']
            target_obj.save_typetree(tree);sprite_count+=1
            path_map[source_obj.path_id]=target_obj.path_id
    # Preserve native script references. Copy only payload fields changed by
    # the translator, including revised testimony/objection animation timing.
    metadata=[]
    if animation_fields:
        target_monos = native_objects(base, 'MonoBehaviour')
        translated_monos = native_objects(ru, 'MonoBehaviour')
        for entry in animation_fields:
            name = entry['name'].casefold()
            target, tree = target_monos[name]
            translated = translated_monos[name][1]
            for field in entry['fields']:
                if field.startswith('m_') or field not in tree:
                    raise ValueError('Unknown native animation field '+field)
                tree[field] = copy.deepcopy(translated[field])
            target.save_typetree(tree)
            metadata.append(entry)
    if original is not None:
        pc_objects={x.path_id:x for x in original.objects}
        pc_monos=native_objects(original,'MonoBehaviour')
        target_monos=native_objects(base,'MonoBehaviour')
        for obj in ru.objects:
            if obj.type.name!='MonoBehaviour':continue
            before=pc_objects.get(obj.path_id)
            if before is None:
                name=obj.read_typetree()['m_Name'].casefold()
                before=pc_monos.get(name,(None,None))[0]
            if before is None:raise ValueError('Unexpected translated script object')
            if obj.get_raw_data()==before.get_raw_data():continue
            changed=obj.read_typetree();old=before.read_typetree();key=changed['m_Name'].casefold()
            if key not in target_monos:raise ValueError('Missing native animation '+key)
            target,tree=target_monos[key];fields=[]
            for name,value in changed.items():
                if name.startswith('m_') or value==old.get(name):continue
                if name not in tree:raise ValueError('Unknown native field '+name)
                tree[name]=copy.deepcopy(value);fields.append(name)
            target.save_typetree(tree);metadata.append({'name':changed['m_Name'],'fields':fields})
    bundle=next(x for x in base.files.values() if type(x).__name__=='BundleFile')
    asset=base.assets[0];old_name=asset.name
    new_name='CAB-'+hashlib.sha256(('Russian/'+rel).encode()+hashlib.sha256(raw).digest()).hexdigest()[:32]
    for obj in base.objects:
        if obj.type.name=='AssetBundle':
            tree=reader.read_object(obj)[0];tree['m_Name']='russian/'+rel.lower()
            if is_new:
                source_bundle=reader.read_object(next(o for o in ru.objects if o.type.name=='AssetBundle'))[0]
                def remap(value):
                    if isinstance(value,dict):
                        value=copy.deepcopy(value)
                        if 'm_FileID' in value and 'm_PathID' in value and value['m_PathID']:
                            if value['m_FileID']!=0 or value['m_PathID'] not in path_map:raise ValueError('Unmapped new bank reference')
                            value['m_PathID']=path_map[value['m_PathID']]
                        else:value={k:remap(v) for k,v in value.items()}
                        return value
                    if isinstance(value,(list,tuple)):return [remap(v) for v in value]
                    return value
                for field in ('m_PreloadTable','m_Container','m_MainAsset'):
                    if field in source_bundle:tree[field]=remap(source_bundle[field])
            obj.save_typetree(tree)
        elif obj.type.name=='Texture2D' and obj.path_id not in converted_ids:
            tree=obj.read_typetree();stream=tree.get('m_StreamData',{})
            if old_name in stream.get('path',''):
                stream['path']=stream['path'].replace(old_name,new_name);obj.save_typetree(tree)
    for external in asset.externals:
        if old_name in external.path:external.path=external.path.replace(old_name,new_name)
    asset.name=new_name
    for key,value in list(bundle.files.items()):
        if old_name not in key:continue
        del bundle.files[key];new_key=key.replace(old_name,new_name);bundle.files[new_key]=value
        if hasattr(value,'name'):value.name=new_key
    encoded=bundle.save(packer='lz4hc')
    # Validate actual serialized data and native platform after rewriting.
    # Re-enable the normal AssetBundle parser for output validation: the repair
    # shim is strictly for reading the translator's nonstandard input records.
    import UnityPy
    check=UnityPy.load(encoded)
    assert {int(x.target_platform) for x in check.assets}=={30}
    assert len([o for o in check.objects if o.type.name=='Sprite'])==len(native_sprites)
    return encoded,{'textures':textures,'sprites':sprite_count,'animation_payloads':metadata,'cab':new_name,'new_sprite':is_new}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--pc',type=Path,required=True)
    parser.add_argument('--native',type=Path,default=reader.root/'Assets/StreamingAssets')
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--retry-errors',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    report={'converted':[],'skipped':[],'errors':[]};started=time.monotonic();retry=None
    if args.retry_errors:
        report=json.loads((args.output/'ConversionReport.json').read_text(encoding='utf-8'))
        retry={x['path'] for x in report['errors']};report['errors']=[]
    with zipfile.ZipFile(args.archive) as archive:
        names=[x for x in archive.namelist() if x.startswith(PREFIX) and x.endswith('.unity3d')]
        if retry is not None:names=[x for x in names if x[len(PREFIX):] in retry]
        if args.limit:names=names[:args.limit]
        for i,name in enumerate(names,1):
            rel=name[len(PREFIX):]
            if rel in SKIP:report['skipped'].append({'path':rel,'reason':'PC/PS4-only button bank; Vita uses native Xbox-layout bank plus gray Cross resources'});continue
            try:
                raw=archive.read(name);dest=args.output/'Russian'/rel
                encoded,details=convert_bundle(rel,raw,args.pc,args.native)
                dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(encoded)
                report['converted'].append(dict(path=rel,bytes=len(encoded),sha256=hashlib.sha256(encoded).hexdigest(),**details))
            except Exception as ex:report['errors'].append({'path':rel,'error':type(ex).__name__+': '+str(ex)})
            gc.collect()
            report['seconds']=round(time.monotonic()-started,2)
            (args.output/'ConversionReport.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            if i%10==0:print('Russian native bundles:',i,'/',len(names),'converted',len(report['converted']),'errors',len(report['errors']),flush=True)
    print('DONE',len(report['converted']),'converted;',len(report['errors']),'errors;',report['seconds'],'seconds',flush=True)
    return bool(report['errors'])

if __name__=='__main__':raise SystemExit(main())
