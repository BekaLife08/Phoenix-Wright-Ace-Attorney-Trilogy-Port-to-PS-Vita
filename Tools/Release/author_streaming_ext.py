"""Author metadata-only recipes for the remaining ~315 StreamingAssets bundles."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from patcher.graphics import read_bundle
from patcher.streaming_ext import (
    ALLOWED_TEX_FORMATS,
    CONVERTERS,
    SPRITE_FIELDS,
    SUPPORTED_PLATFORMS,
)


UNUSED_MOVIES = {
    'gs1/movie/film00.unity3d',
    'gs1/movie/film03.unity3d',
    'gs1/movie/videonoise.unity3d',
    'gs1/moviescriptable/film01_confront.unity3d',
    'gs1/moviescriptable/film02_confront.unity3d',
}

CUSTOM_VITA_UI = {
    'menu/common/court_record_03_xbox.unity3d',
    'menu/common/court_record_03_xboxb.unity3d',
    'menu/common/court_record_03_xboxf.unity3d',
    'menu/common/court_record_03_xboxg.unity3d',
    'menu/common/court_record_03_xboxk.unity3d',
    'menu/common/court_record_03_xboxl.unity3d',
    'menu/common/court_record_03_xboxs.unity3d',
    'menu/common/court_record_03_xboxt.unity3d',
    'menu/common/court_record_03_xboxu.unity3d',
    'menu/common/symbol_xbox.unity3d',
    'menu/option/option_list_bg_7.unity3d',
    'menu/option/option_list_bg_8.unity3d',
    'menu/option/option_list_bg_9.unity3d',
    'menu/option/symbol_xbox_large.unity3d',
    'menu/title/titleu.unity3d',
}

SOURCE_ALIASES = {
    'GS1/etc/etc00ak_0.unity3d': 'GS1/etc/etc00ak.unity3d',
}


def _address_by_map(meta, id_to_ref):
    def addr(ptr):
        if ptr['m_FileID']:
            raise ValueError('External reference in bundle metadata')
        return list(id_to_ref[ptr['m_PathID']]) if ptr['m_PathID'] else None

    return {
        'name': meta['m_Name'],
        'bundle_name': meta['m_AssetBundleName'],
        'container': [[name, info['preloadIndex'], info['preloadSize'], addr(info['asset'])]
                      for name, info in meta['m_Container']],
        'preload': [addr(p) for p in meta['m_PreloadTable']],
        'main': [meta['m_MainAsset']['preloadIndex'], meta['m_MainAsset']['preloadSize'],
                 addr(meta['m_MainAsset']['asset'])],
    }


def _tex_spec(vt, src_name):
    return {
        'source_name': src_name,
        'name': vt['m_Name'],
        'width': vt['m_Width'],
        'height': vt['m_Height'],
        'format': vt['m_TextureFormat'],
        'mip_count': vt['m_MipCount'],
        'color_space': vt['m_ColorSpace'],
        'is_readable': vt['m_IsReadable'],
        'lightmap_format': vt['m_LightmapFormat'],
        'settings': vt['m_TextureSettings'],
    }


def build_graphics_ext_entry(rel, source_rel, steam_bytes, vita_bytes):
    senv = read_bundle(steam_bytes)
    venv = read_bundle(vita_bytes)
    if len(senv.assets) != 1 or int(senv.assets[0].target_platform) not in SUPPORTED_PLATFORMS:
        raise ValueError('Unsupported Steam platform')
    if len(venv.assets) != 1 or int(venv.assets[0].target_platform) != 30:
        raise ValueError('Unsupported Vita platform')

    s_by_kind = {}
    for o in senv.objects:
        if o.type.name in ('Texture2D', 'Sprite'):
            s_by_kind.setdefault((o.type.name, o.peek_name().casefold()), []).append(o)

    v_bundle = None
    v_texs, v_sprs = [], []
    for o in venv.objects:
        k = o.type.name
        if k == 'AssetBundle':
            v_bundle = o.read_typetree()
        elif k == 'Texture2D':
            v_texs.append(o)
        elif k == 'Sprite':
            v_sprs.append(o)
        else:
            raise ValueError('Unsupported class in graphics_ext: ' + k)

    # Deduplicate Vita sprites if AssetRipper created identical duplicate sprites (e.g. copygs1)
    # Match each Vita Texture2D and Sprite to a unique Steam object
    id_to_ref = {}
    tex_specs = []
    used_s_tex = set()
    for o in v_texs:
        vt = o.read_typetree()
        if vt['m_TextureFormat'] not in ALLOWED_TEX_FORMATS:
            raise ValueError(f"Unsupported Vita texture format: {vt['m_TextureFormat']}")
        key = ('Texture2D', vt['m_Name'].casefold())
        cands = [so for so in s_by_kind.get(key, []) if so.path_id not in used_s_tex]
        if not cands and len(v_texs) == 1:
            all_st = [so for (k, _), lst in s_by_kind.items() if k == 'Texture2D' for so in lst if so.path_id not in used_s_tex]
            if len(all_st) == 1:
                cands = all_st
        if not cands:
            raise ValueError(f"No matching Steam Texture2D for {vt['m_Name']}")
        so = cands[0]
        used_s_tex.add(so.path_id)
        idx = len(tex_specs)
        tex_specs.append(_tex_spec(vt, so.peek_name()))
        id_to_ref[o.path_id] = ('Texture2D', idx)

    spr_specs = []
    used_s_spr = set()
    spr_by_name_ref = {}
    for o in v_sprs:
        vt = o.read_typetree()
        key = ('Sprite', vt['m_Name'].casefold())
        cands = [so for so in s_by_kind.get(key, []) if so.path_id not in used_s_spr]
        if not cands and len(v_sprs) == 1:
            all_ss = [so for (k, _), lst in s_by_kind.items() if k == 'Sprite' for so in lst if so.path_id not in used_s_spr]
            if len(all_ss) == 1:
                cands = all_ss
        if not cands:
            # Try stripping trailing _0 or matching title_textu -> title_text
            alt_names = {vt['m_Name'].casefold().removesuffix('_0')}
            alt_names.add(vt['m_Name'].casefold().replace('title_textu', 'title_text').removesuffix('_0'))
            for alt in alt_names:
                cands = [so for so in s_by_kind.get(('Sprite', alt), []) if so.path_id not in used_s_spr]
                if cands:
                    break
        if not cands and len(v_sprs) == sum(len(v) for (k, _), v in s_by_kind.items() if k == 'Sprite'):
            # Fallback: match by closest m_Rect among remaining Steam sprites
            rem = [so for (k, _), lst in s_by_kind.items() if k == 'Sprite' for so in lst if so.path_id not in used_s_spr]
            if rem:
                vr = vt['m_Rect']
                def rect_dist(so):
                    sr = so.read_typetree()['m_Rect']
                    return abs(sr['x'] - vr['x']) + abs(sr['y'] - vr['y']) + abs(sr['width'] - vr['width']) + abs(sr['height'] - vr['height'])
                cands = [min(rem, key=rect_dist)]
        if not cands:
            # Handle AssetRipper duplicate Sprite (e.g. copyGS1 appears twice in Vita, once in Steam)
            if vt['m_Name'].casefold() in spr_by_name_ref:
                id_to_ref[o.path_id] = spr_by_name_ref[vt['m_Name'].casefold()]
                continue
            raise ValueError(f"No matching Steam Sprite for {vt['m_Name']}")
        so = cands[0]
        used_s_spr.add(so.path_id)
        spec = {'source_name': so.peek_name(), 'name': vt['m_Name']}
        for field in SPRITE_FIELDS:
            spec[field] = vt[field]
        spec['rd_rect'] = vt['m_RD']['textureRect']
        spec['rd_offset'] = vt['m_RD']['textureRectOffset']
        spec['rd_settings'] = vt['m_RD']['settingsRaw']
        idx = len(spr_specs)
        spr_specs.append(spec)
        ref = ('Sprite', idx)
        id_to_ref[o.path_id] = ref
        spr_by_name_ref[vt['m_Name'].casefold()] = ref

    entry = {
        'kind': 'graphics_ext',
        'path': rel,
        'sha256': hashlib.sha256(steam_bytes).hexdigest(),
        'textures': tex_specs,
        'sprites': spr_specs,
        **_address_by_map(v_bundle, id_to_ref),
    }
    if source_rel != rel:
        entry['source'] = source_rel
    return entry


def _mb_sort_key(obj, script_names):
    tree = obj.read_typetree()
    script_id = tree['m_Script']['m_PathID']
    cls = script_names.get(script_id, '')
    canon = tree['m_Name'].casefold().removeprefix('sound_')
    return (cls, canon, obj.path_id)


def build_atlas_ext_entry(rel, steam_bytes, vita_bytes):
    senv = read_bundle(steam_bytes)
    venv = read_bundle(vita_bytes)
    s_scripts = [o for o in senv.objects if o.type.name == 'MonoScript']
    v_scripts = [o for o in venv.objects if o.type.name == 'MonoScript']
    s_script_names = {o.path_id: o.read_typetree().get('m_ClassName', o.peek_name()) for o in s_scripts}
    v_script_names = {o.path_id: o.read_typetree().get('m_ClassName', o.peek_name()) for o in v_scripts}
    s_scripts.sort(key=lambda o: (s_script_names[o.path_id].casefold(), o.path_id))
    v_scripts.sort(key=lambda o: (v_script_names[o.path_id].casefold(), o.path_id))
    s_mbs = sorted([o for o in senv.objects if o.type.name == 'MonoBehaviour'], key=lambda o: _mb_sort_key(o, s_script_names))
    v_mbs = sorted([o for o in venv.objects if o.type.name == 'MonoBehaviour'], key=lambda o: _mb_sort_key(o, v_script_names))
    if len(s_mbs) != len(v_mbs) or len(s_scripts) != len(v_scripts):
        raise ValueError(f'Atlas object count mismatch: mb {len(s_mbs)}/{len(v_mbs)} ms {len(s_scripts)}/{len(v_scripts)}')
    for so, vo in zip(s_mbs, v_mbs):
        sc = s_script_names.get(so.read_typetree()['m_Script']['m_PathID'], '')
        vc = v_script_names.get(vo.read_typetree()['m_Script']['m_PathID'], '')
        if sc != vc:
            raise ValueError(f'Atlas MonoBehaviour script mismatch: {sc} vs {vc}')

    v_bundle = [o.read_typetree() for o in venv.objects if o.type.name == 'AssetBundle'][0]
    v_tex_obj = [o for o in venv.objects if o.type.name == 'Texture2D'][0]
    s_tex_obj = [o for o in senv.objects if o.type.name == 'Texture2D'][0]
    v_size_objs = [o for o in venv.objects if o.type.name == 'TextAsset' and o.peek_name() == 'vita_atlas_size']

    id_to_ref = {v_tex_obj.path_id: ('Texture2D', 0)}
    for i, o in enumerate(v_mbs):
        id_to_ref[o.path_id] = ('MonoBehaviour', i)
    for i, o in enumerate(v_scripts):
        id_to_ref[o.path_id] = ('MonoScript', i)
    if v_size_objs:
        id_to_ref[v_size_objs[0].path_id] = ('TextAsset', 0)

    entry = {
        'kind': 'atlas_ext',
        'path': rel,
        'sha256': hashlib.sha256(steam_bytes).hexdigest(),
        'texture': _tex_spec(v_tex_obj.read_typetree(), s_tex_obj.peek_name()),
        'size_marker': bool(v_size_objs),
        **_address_by_map(v_bundle, id_to_ref),
    }
    mb_specs = [[v_script_names.get(o.read_typetree()['m_Script']['m_PathID'], ''), o.peek_name()] for o in v_mbs]
    if [name for _, name in mb_specs] != [o.peek_name() for o in s_mbs]:
        entry['mb_names'] = mb_specs
    return entry


def build_foa_dict_entry(rel, steam_bytes, vita_bytes):
    venv = read_bundle(vita_bytes)
    v_bundle = [o.read_typetree() for o in venv.objects if o.type.name == 'AssetBundle'][0]
    v_mb = [o for o in venv.objects if o.type.name == 'MonoBehaviour'][0]
    v_ms = [o for o in venv.objects if o.type.name == 'MonoScript'][0]
    id_to_ref = {v_mb.path_id: ('MonoBehaviour', 0), v_ms.path_id: ('MonoScript', 0)}
    return {
        'kind': 'foa_dict',
        'path': rel,
        'sha256': hashlib.sha256(steam_bytes).hexdigest(),
        'replaces': v_mb.read_typetree()['replaces'],
        **_address_by_map(v_bundle, id_to_ref),
    }


def build_science_3d_entry(rel, steam_bytes, vita_bytes):
    venv = read_bundle(vita_bytes)
    if any(o.type.name == 'Shader' for o in venv.objects):
        raise ValueError('Contains compiled Shader object')
    v_bundle = [o.read_typetree() for o in venv.objects if o.type.name == 'AssetBundle'][0]
    # Determine which vertex channels Vita kept across meshes
    kept = set()
    for o in venv.objects:
        if o.type.name == 'Mesh':
            t = o.read_typetree()
            for idx, ch in enumerate(t['m_VertexData']['m_Channels']):
                if ch['dimension'] > 0:
                    kept.add(idx)
    return {
        'kind': 'science_3d',
        'path': rel,
        'sha256': hashlib.sha256(steam_bytes).hexdigest(),
        'name': v_bundle['m_Name'],
        'bundle_name': v_bundle['m_AssetBundleName'],
        'keep_channels': sorted(kept),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steam', type=Path, required=True)
    parser.add_argument('--media-zip', type=Path, required=True)
    parser.add_argument('--completion-patch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    with ZipFile(args.completion_patch) as pz:
        manifest = json.loads(pz.read('manifest.json'))
    sa_paths = [
        path[len('StreamingAssets/'):]
        for g in manifest['groups']
        for path, _, _ in g['targets']
        if path.startswith('StreamingAssets/')
    ]

    entries = []
    skipped = Counter()
    failed = []
    with ZipFile(args.media_zip) as mz:
        for rel in sorted(sa_paths):
            low = rel.lower()
            if low in UNUSED_MOVIES:
                skipped['unused_movie'] += 1
                continue
            if low in CUSTOM_VITA_UI:
                skipped['custom_vita_ui'] += 1
                continue
            source_rel = SOURCE_ALIASES.get(rel, rel)
            steam_path = args.steam / 'StreamingAssets' / source_rel
            if not steam_path.is_file():
                failed.append((rel, 'missing in Steam'))
                continue
            steam_bytes = steam_path.read_bytes()
            vita_bytes = mz.read('Media/StreamingAssets/' + rel)
            try:
                venv = read_bundle(vita_bytes)
                vtypes = {o.type.name for o in venv.objects}
                if 'TextureAnmRelations' in rel:
                    entry = build_atlas_ext_entry(rel, steam_bytes, vita_bytes)
                elif rel.endswith('foareplacementcharacters.unity3d'):
                    entry = build_foa_dict_entry(rel, steam_bytes, vita_bytes)
                elif rel.startswith('GS1/science/'):
                    entry = build_science_3d_entry(rel, steam_bytes, vita_bytes)
                elif vtypes <= {'AssetBundle', 'Texture2D', 'Sprite'}:
                    entry = build_graphics_ext_entry(rel, source_rel, steam_bytes, vita_bytes)
                else:
                    raise ValueError(f'Unhandled types: {sorted(vtypes)}')
                encoded = CONVERTERS[entry['kind']](steam_bytes, entry)
                entry['output_sha256'] = hashlib.sha256(encoded).hexdigest()
                entries.append(entry)
            except Exception as exc:
                failed.append((rel, str(exc)))

    payload = {'format': 'pwaat-portable-streaming-ext/1', 'files': entries}
    args.output.write_text(json.dumps(payload, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Authored {len(entries)} extended streaming entries -> {args.output} ({args.output.stat().st_size / 1024:.1f} KB)')
    print('Skipped:', dict(skipped))
    print(f'Failed: {len(failed)}')
    for rel, reason in failed:
        print(f'  FAIL {rel}: {reason}')


if __name__ == '__main__':
    main()
