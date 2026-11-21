"""Extended local Steam->PSP2 converters for remaining StreamingAssets bundles.

Handles:
- graphics_ext: Texture2D + Sprite bundles with case/name differences, RGB24,
  resizing, mipmap changes, tight/custom Sprite metadata, platform 5, or
  source path aliasing (e.g. etc00ak -> etc00ak_0).
- atlas_ext: TextureAnmRelations bundles with duplicate MonoBehaviour names,
  case differences, texture-only atlases, or extra Steam Sprite objects.
- foa_dict: AnimationDictionaries/foareplacementcharacters with Vita updates.
- science_3d: GS1/science 3D evidence bundles (retargeting platform=30, inlining
  Texture2D streams, and stripping unused Mesh vertex channels to match PSP2).
"""

import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile

from PIL import Image
from UnityPy.enums import BuildTarget, ClassIDType
from UnityPy.helpers.Tpk import get_typetree_node

from .atlas import ATLAS_SIZE, TEXT_ASSET_HASH, add_size
from .core import PatchError, check_cancel, safe_relative, sha256
from .graphics import read_bundle


SUPPORTED_PLATFORMS = (5, 19)
SPRITE_FIELDS = ('m_Rect', 'm_Offset', 'm_Border', 'm_PixelsToUnits', 'm_Pivot', 'm_Extrude', 'm_IsPolygon')
ALLOWED_TEX_FORMATS = (3, 4, 10, 12)


def _apply_addresses_by_key(bundle_obj, metadata, obj_map, entry):
    def pointer(ref):
        if ref is None:
            return {'m_FileID': 0, 'm_PathID': 0}
        return {'m_FileID': 0, 'm_PathID': obj_map[tuple(ref)].path_id}

    def asset_info(start, count, ref):
        return {'preloadIndex': start, 'preloadSize': count, 'asset': pointer(ref)}

    metadata['m_Name'] = entry['name']
    metadata['m_AssetBundleName'] = entry['bundle_name']
    metadata['m_Container'] = [(name, asset_info(start, count, ref))
                               for name, start, count, ref in entry['container']]
    metadata['m_PreloadTable'] = [pointer(ref) for ref in entry['preload']]
    metadata['m_MainAsset'] = asset_info(*entry['main'])
    bundle_obj.save_typetree(metadata)


def _finalize_bundle(env, tag, rel_path):
    bundle_file = env.file
    for name in list(bundle_file.files):
        if name.endswith('.resS'):
            del bundle_file.files[name]
    asset = env.assets[0]
    old_name = asset.name
    asset.name = 'CAB-' + hashlib.sha256(f'{tag}/{rel_path.lower()}'.encode()).hexdigest()[:32]
    bundle_file.files[asset.name] = bundle_file.files.pop(old_name)
    asset.target_platform = BuildTarget.PSP2
    asset._m_target_platform = int(BuildTarget.PSP2)
    return bundle_file.save(packer='lz4')


def _update_texture(obj, spec):
    tex = obj.read()
    target_size = (spec['width'], spec['height'])
    target_fmt = spec['format']
    target_mips = spec['mip_count']
    if target_fmt not in ALLOWED_TEX_FORMATS:
        raise ValueError(f'Unsupported texture format: {target_fmt}')
    same_dims = (tex.m_Width, tex.m_Height) == target_size
    same_fmt = tex.m_TextureFormat == target_fmt
    same_mips = tex.m_MipCount == target_mips
    if same_dims and same_fmt and same_mips:
        tex.image_data = tex.get_image_data()
        tex.m_StreamData.path = ''
        tex.m_StreamData.offset = 0
        tex.m_StreamData.size = 0
    else:
        img = tex.image
        if not same_dims:
            img = img.resize(target_size, Image.Resampling.LANCZOS)
        tex.set_image(img, target_format=target_fmt, mipmap_count=target_mips)
    tex.m_Name = spec['name']
    tex.m_IsReadable = spec['is_readable']
    tex.m_ColorSpace = spec['color_space']
    if 'lightmap_format' in spec:
        tex.m_LightmapFormat = spec['lightmap_format']
    if 'settings' in spec:
        s = spec['settings']
        ts = tex.m_TextureSettings
        for attr in ('m_FilterMode', 'm_Aniso', 'm_MipBias', 'm_WrapU', 'm_WrapV', 'm_WrapW', 'm_WrapMode'):
            if attr in s and hasattr(ts, attr):
                setattr(ts, attr, s[attr])
    tex.save()
    check = obj.read()
    if not check.get_image_data():
        raise ValueError(f"Texture {spec['name']} lost image data during conversion")


def convert_graphics_ext(data, entry):
    env = read_bundle(data)
    if len(env.assets) != 1 or int(env.assets[0].target_platform) not in SUPPORTED_PLATFORMS:
        raise ValueError('Unexpected Steam graphics platform')
    bundle_obj, bundle_meta = None, None
    by_kind_lower = {}
    for obj in env.objects:
        kind = obj.type.name
        if kind == 'AssetBundle':
            bundle_obj, bundle_meta = obj, obj.read_typetree()
        elif kind in ('Texture2D', 'Sprite'):
            name = obj.peek_name()
            by_kind_lower.setdefault((kind, name.casefold()), []).append(obj)
        else:
            raise ValueError('Unexpected asset class in graphics_ext: ' + kind)
    if bundle_obj is None:
        raise ValueError('Missing AssetBundle object')

    obj_map = {}
    # Match and convert Texture2D objects
    used_tex = set()
    for idx, spec in enumerate(entry['textures']):
        key = ('Texture2D', spec['source_name'].casefold())
        candidates = [o for o in by_kind_lower.get(key, []) if o.path_id not in used_tex]
        if not candidates:
            raise ValueError(f"Missing Steam Texture2D: {spec['source_name']}")
        obj = candidates[0]
        used_tex.add(obj.path_id)
        _update_texture(obj, spec)
        obj_map[('Texture2D', idx)] = obj

    # Match and update Sprite objects
    used_spr = set()
    for idx, spec in enumerate(entry['sprites']):
        key = ('Sprite', spec['source_name'].casefold())
        candidates = [o for o in by_kind_lower.get(key, []) if o.path_id not in used_spr]
        if not candidates:
            raise ValueError(f"Missing Steam Sprite: {spec['source_name']}")
        obj = candidates[0]
        used_spr.add(obj.path_id)
        tree = obj.read_typetree()
        tree['m_Name'] = spec['name']
        for field in SPRITE_FIELDS:
            if field in spec:
                tree[field] = spec[field]
        if 'rd_rect' in spec:
            tree['m_RD']['textureRect'] = spec['rd_rect']
        if 'rd_offset' in spec:
            tree['m_RD']['textureRectOffset'] = spec['rd_offset']
        if 'rd_settings' in spec:
            tree['m_RD']['settingsRaw'] = spec['rd_settings']
        obj.save_typetree(tree)
        obj_map[('Sprite', idx)] = obj

    _apply_addresses_by_key(bundle_obj, bundle_meta, obj_map, entry)
    return _finalize_bundle(env, 'portable-graphics-ext', entry['path'])


def _mb_sort_key(obj, script_names):
    tree = obj.read_typetree()
    script_id = tree['m_Script']['m_PathID']
    cls = script_names.get(script_id, '')
    canon = tree['m_Name'].casefold().removeprefix('sound_')
    return (cls, canon, obj.path_id)


def convert_atlas_ext(data, entry):
    env = read_bundle(data)
    if len(env.assets) != 1 or int(env.assets[0].target_platform) not in SUPPORTED_PLATFORMS:
        raise ValueError('Unexpected Steam atlas platform')
    bundle_obj, bundle_meta = None, None
    tex_obj = None
    mbs = []
    scripts = []
    for obj in env.objects:
        kind = obj.type.name
        if kind == 'AssetBundle':
            bundle_obj, bundle_meta = obj, obj.read_typetree()
        elif kind == 'Texture2D':
            if tex_obj is not None:
                raise ValueError('Multiple Texture2D in atlas_ext')
            tex_obj = obj
        elif kind == 'MonoBehaviour':
            mbs.append(obj)
        elif kind == 'MonoScript':
            scripts.append(obj)
        elif kind == 'Sprite':
            # Unused Sprite in Steam (e.g. animationset_etc00ak) - left unreferenced
            pass
        else:
            raise ValueError('Unexpected class in atlas_ext: ' + kind)
    if bundle_obj is None or tex_obj is None:
        raise ValueError('Missing AssetBundle or Texture2D in atlas_ext')

    orig_tex = tex_obj.read()
    orig_size = (orig_tex.m_Width, orig_tex.m_Height)
    _update_texture(tex_obj, entry['texture'])

    # Map objects deterministically by (MonoScript class, canonical name without 'sound_' prefix, path_id)
    script_names = {o.path_id: o.read_typetree().get('m_ClassName', o.peek_name()) for o in scripts}
    scripts.sort(key=lambda o: (script_names[o.path_id].casefold(), o.path_id))
    mbs.sort(key=lambda o: _mb_sort_key(o, script_names))

    if 'mb_names' in entry:
        if len(entry['mb_names']) != len(mbs):
            raise ValueError('MonoBehaviour count mismatch in atlas_ext')
        for obj, spec in zip(mbs, entry['mb_names']):
            if isinstance(spec, list):
                expected_cls, new_name = spec
                actual_cls = script_names.get(obj.read_typetree()['m_Script']['m_PathID'], '')
                if actual_cls != expected_cls:
                    raise ValueError(f'MonoBehaviour script class mismatch: {actual_cls} != {expected_cls}')
            else:
                new_name = spec
            if obj.peek_name() != new_name:
                t = obj.read_typetree()
                t['m_Name'] = new_name
                obj.save_typetree(t)

    obj_map = {('Texture2D', 0): tex_obj}
    for i, o in enumerate(mbs):
        obj_map[('MonoBehaviour', i)] = o
    for i, o in enumerate(scripts):
        obj_map[('MonoScript', i)] = o

    asset = env.assets[0]
    if entry.get('size_marker'):
        size_obj, _ = add_size(asset, tex_obj, orig_size)
        obj_map[('TextAsset', 0)] = size_obj

    _apply_addresses_by_key(bundle_obj, bundle_meta, obj_map, entry)
    return _finalize_bundle(env, 'portable-atlas-ext', entry['path'])


def convert_foa_dict(data, entry):
    env = read_bundle(data)
    if len(env.assets) != 1 or int(env.assets[0].target_platform) not in SUPPORTED_PLATFORMS:
        raise ValueError('Unexpected Steam foa_dict platform')
    bundle_obj, bundle_meta = None, None
    mb_obj, ms_obj = None, None
    for obj in env.objects:
        kind = obj.type.name
        if kind == 'AssetBundle':
            bundle_obj, bundle_meta = obj, obj.read_typetree()
        elif kind == 'MonoBehaviour':
            mb_obj = obj
        elif kind == 'MonoScript':
            ms_obj = obj
    if not (bundle_obj and mb_obj and ms_obj):
        raise ValueError('Incomplete foa_dict bundle')
    tree = mb_obj.read_typetree()
    tree['replaces'] = entry['replaces']
    mb_obj.save_typetree(tree)
    obj_map = {('MonoBehaviour', 0): mb_obj, ('MonoScript', 0): ms_obj}
    _apply_addresses_by_key(bundle_obj, bundle_meta, obj_map, entry)
    return _finalize_bundle(env, 'portable-foa-dict', entry['path'])


def _strip_mesh_channels(mesh_tree, keep_channels):
    """Strip unused float32 vertex channels from a single-stream Unity Mesh."""
    vd = mesh_tree['m_VertexData']
    vcount = vd['m_VertexCount']
    if vcount == 0 or mesh_tree.get('m_MeshCompression', 0) != 0:
        return
    channels = vd['m_Channels']
    old_data = bytes(vd['m_DataSize'])
    if not old_data:
        return
    old_stride = len(old_data) // vcount
    if old_stride * vcount != len(old_data):
        return
    # Compute slices to keep in channel order
    slices = []
    new_channels = []
    new_offset = 0
    for idx, ch in enumerate(channels):
        dim = ch['dimension']
        fmt = ch['format']
        if idx in keep_channels and dim > 0:
            elem_bytes = 4 if fmt == 0 else (2 if fmt == 1 else 1)
            byte_len = dim * elem_bytes
            slices.append((ch['offset'], ch['offset'] + byte_len))
            new_channels.append({'stream': 0, 'offset': new_offset, 'format': fmt, 'dimension': dim})
            new_offset += byte_len
        else:
            new_channels.append({'stream': 0, 'offset': 0, 'format': 0, 'dimension': 0})
    if new_offset == old_stride:
        return
    out = bytearray(vcount * new_offset)
    dst = 0
    for v in range(vcount):
        base = v * old_stride
        for s0, s1 in slices:
            n = s1 - s0
            out[dst:dst + n] = old_data[base + s0:base + s1]
            dst += n
    vd['m_Channels'] = new_channels
    vd['m_DataSize'] = bytes(out)


def convert_science_3d(data, entry):
    env = read_bundle(data)
    if len(env.assets) != 1 or int(env.assets[0].target_platform) not in SUPPORTED_PLATFORMS:
        raise ValueError('Unexpected Steam science_3d platform')
    for obj in env.objects:
        kind = obj.type.name
        if kind == 'Texture2D':
            tex = obj.read()
            tex.image_data = tex.get_image_data()
            tex.m_StreamData.path = ''
            tex.m_StreamData.offset = 0
            tex.m_StreamData.size = 0
            tex.save()
        elif kind == 'Mesh':
            tree = obj.read_typetree()
            keep = entry.get('keep_channels')
            if keep is not None:
                _strip_mesh_channels(tree, set(keep))
                obj.save_typetree(tree)
        elif kind == 'AssetBundle':
            tree = obj.read_typetree()
            tree['m_Name'] = entry['name']
            tree['m_AssetBundleName'] = entry['bundle_name']
            obj.save_typetree(tree)
    return _finalize_bundle(env, 'portable-science-3d', entry['path'])


CONVERTERS = {
    'graphics_ext': convert_graphics_ext,
    'atlas_ext': convert_atlas_ext,
    'foa_dict': convert_foa_dict,
    'science_3d': convert_science_3d,
}


def prepare_streaming_ext(streaming, output, recipe_path, progress=None, cancel=None, *, excluded=()):
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    if recipe.get('format') != 'pwaat-portable-streaming-ext/1':
        raise PatchError('Unsupported extended streaming recipe.')
    files = [entry for entry in recipe['files'] if entry['path'].lower() not in excluded]
    processed = set()
    for index, entry in enumerate(files, 1):
        check_cancel(cancel)
        relative = safe_relative(entry['path'])
        source_rel = safe_relative(entry.get('source', relative))
        source = streaming / source_rel
        target = output / 'Media/StreamingAssets' / relative
        if not source.resolve().is_relative_to(streaming) or not target.resolve().is_relative_to(output):
            raise PatchError('Extended streaming path points outside the selected folder.')
        try:
            if target.is_file() and sha256(target) == entry['output_sha256']:
                processed.add(relative.lower())
                if progress:
                    progress('Extended PSP2 assets', index, len(files), relative)
                continue
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Unsupported Steam file version')
            converter = CONVERTERS[entry['kind']]
            encoded = converter(raw, entry)
            if hashlib.sha256(encoded).hexdigest() != entry['output_sha256']:
                raise ValueError('Output does not match verified conversion')
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                tmp_path = Path(tmp.name)
                try:
                    tmp.write(encoded)
                    tmp.close()
                    tmp_path.replace(target)
                finally:
                    tmp.close()
                    tmp_path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError) as error:
            raise PatchError(f'Extended bundle {relative}: {error}') from error
        processed.add(relative.lower())
        if progress:
            progress('Extended PSP2 assets', index, len(files), relative)
    return processed
