"""Convert texture-only bundles whose layout matches the Vita reference."""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import UnityPy
from UnityPy.enums import BuildTarget

from .audio import _plain
from .core import PatchError, check_cancel, sha256


KINDS = {'AssetBundle', 'Texture2D', 'Sprite'}
TEXTURE_FIELDS = ('m_Width', 'm_Height', 'm_ColorSpace', 'm_TextureSettings', 'm_IsReadable')
SPRITE_FIELDS = ('m_Rect', 'm_Offset', 'm_Border', 'm_PixelsToUnits', 'm_Pivot', 'm_Extrude', 'm_IsPolygon')


def read_bundle(data):
    return UnityPy.load(_plain(data))


def objects(env, platform):
    if (len(env.assets) != 1 or int(env.assets[0].target_platform) != platform
            or env.assets[0].externals or env.assets[0].script_types):
        raise ValueError('Unexpected serialized file layout')
    objects_by_name = {}
    bundle = None
    for obj in env.objects:
        kind = obj.type.name
        if kind not in KINDS:
            raise ValueError('Unsupported asset type: ' + kind)
        tree = obj.read_typetree()
        if kind == 'AssetBundle':
            if bundle is not None or tree['m_Dependencies'] or tree['m_IsStreamedSceneAssetBundle']:
                raise ValueError('Unsupported bundle dependencies')
            bundle = (obj, tree)
            continue
        key = (kind, tree['m_Name'])
        if key in objects_by_name:
            raise ValueError('Ambiguous object names')
        objects_by_name[key] = (obj, tree)
    if not bundle or not any(k[0] == 'Texture2D' for k in objects_by_name):
        raise ValueError('Bundle has no standalone textures')
    return bundle, objects_by_name


def signature(index):
    ids = {obj.path_id: key for key, (obj, _) in index.items()}
    shape = {}
    for key, (_, tree) in index.items():
        if key[0] == 'Texture2D':
            shape[key] = tuple(tree[name] for name in TEXTURE_FIELDS)
            continue
        render = dict(tree['m_RD'])
        for name in ('texture', 'alphaTexture'):
            pointer = render.pop(name)
            if pointer['m_FileID']:
                raise ValueError('Unsupported external sprite texture')
            render[name] = ids[pointer['m_PathID']] if pointer['m_PathID'] else None
        if tree['m_SpriteAtlas']['m_PathID']:
            raise ValueError('Unsupported sprite atlas')
        shape[key] = (tuple(tree[name] for name in SPRITE_FIELDS), render, tree.get('m_PhysicsShape'))
    return shape


def address_recipe(bundle, index):
    ids = {obj.path_id: list(key) for key, (obj, _) in index.items()}

    def address(pointer):
        if pointer['m_FileID']:
            raise ValueError('Unsupported external asset reference')
        return ids[pointer['m_PathID']] if pointer['m_PathID'] else None

    return {
        'name': bundle['m_Name'],
        'bundle_name': bundle['m_AssetBundleName'],
        'container': [[name, data['preloadIndex'], data['preloadSize'], address(data['asset'])]
                      for name, data in bundle['m_Container']],
        'preload': [address(item) for item in bundle['m_PreloadTable']],
        'main': [bundle['m_MainAsset']['preloadIndex'], bundle['m_MainAsset']['preloadSize'],
                 address(bundle['m_MainAsset']['asset'])],
    }


def tighten_sprites(index):
    """Use the Steam sprite's existing tight bounds while preserving its pixel pivot."""
    for (kind, _), (_, tree) in index.items():
        if kind != 'Sprite':
            continue
        rect = tree['m_Rect']
        render = tree['m_RD']
        tight = render['textureRect']
        for axis, size in (('x', 'width'), ('y', 'height')):
            start, length = tight[axis], tight[size]
            if length <= 0 or start < rect[axis] - 0.001 or start + length > rect[axis] + rect[size] + 0.001:
                raise ValueError('Unsupported tight sprite bounds')
            pivot = rect[axis] + rect[size] * tree['m_Pivot'][axis]
            tree['m_Pivot'][axis] = (pivot - start) / length
            tree['m_Offset'][axis] += rect[axis] + rect[size] / 2 - start - length / 2
            render['textureRectOffset'][axis] -= start - rect[axis]
        tree['m_Rect'] = tight.copy()


def same_geometry(left, right):
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=0, abs_tol=0.00015)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_geometry(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)) and type(left) is type(right):
        return len(left) == len(right) and all(same_geometry(a, b) for a, b in zip(left, right))
    return left == right


def build_recipe(relative, source, native, *, tight_sprites=False):
    _, local = objects(read_bundle(source), 19)
    (_, template), target = objects(read_bundle(native), 30)
    if tight_sprites:
        tighten_sprites(local)
    matches = same_geometry(signature(local), signature(target)) if tight_sprites else signature(local) == signature(target)
    if not matches:
        raise ValueError('Vita texture or Sprite layout differs')
    textures = {}
    for (kind, name), (_, tree) in target.items():
        if kind != 'Texture2D':
            continue
        source_tree = local[kind, name][1]
        fmt, mips = tree['m_TextureFormat'], tree['m_MipCount']
        if fmt not in (4, 10, 12) or source_tree['m_MipCount'] != mips:
            raise ValueError('Unsupported texture compression')
        if fmt != source_tree['m_TextureFormat'] and mips != 1:
            raise ValueError('Cannot re-encode multiple mip levels')
        textures[name] = fmt
    if not textures:
        raise ValueError('Bundle has no textures')
    entry = {'path': relative, 'sha256': hashlib.sha256(source).hexdigest(),
             'textures': textures, **address_recipe(template, target)}
    if tight_sprites:
        entry['tight_sprites'] = True
    return entry


def apply_addresses(bundle, metadata, local, entry):
    def pointer(address):
        return {'m_FileID': 0, 'm_PathID': local[tuple(address)][0].path_id if address else 0}

    def asset_info(start, count, address):
        return {'preloadIndex': start, 'preloadSize': count, 'asset': pointer(address)}

    metadata['m_Name'] = entry['name']
    metadata['m_AssetBundleName'] = entry['bundle_name']
    metadata['m_Container'] = [(name, asset_info(start, count, address))
                               for name, start, count, address in entry['container']]
    metadata['m_PreloadTable'] = [pointer(address) for address in entry['preload']]
    metadata['m_MainAsset'] = asset_info(*entry['main'])
    bundle.save_typetree(metadata)


def convert(data, entry):
    env = read_bundle(data)
    (bundle, metadata), local = objects(env, 19)
    if entry.get('tight_sprites'):
        tighten_sprites(local)
        for (kind, _), (obj, tree) in local.items():
            if kind == 'Sprite':
                obj.save_typetree(tree)
    original = signature(local)
    apply_addresses(bundle, metadata, local, entry)
    for name, fmt in entry['textures'].items():
        obj, _ = local['Texture2D', name]
        texture = obj.read()
        if fmt != texture.m_TextureFormat:
            texture.set_image(texture.image, target_format=fmt, mipmap_count=1)
        else:
            texture.image_data = texture.get_image_data()
            texture.m_StreamData.path = ''
            texture.m_StreamData.offset = 0
            texture.m_StreamData.size = 0
        texture.save()
    bundle_file = env.file
    for name in list(bundle_file.files):
        if name.endswith('.resS'):
            del bundle_file.files[name]
    asset = env.assets[0]
    old_name = asset.name
    asset.name = 'CAB-' + hashlib.sha256(('portable/' + entry['path'].lower()).encode()).hexdigest()[:32]
    bundle_file.files[asset.name] = bundle_file.files.pop(old_name)
    asset.target_platform = BuildTarget.PSP2
    asset._m_target_platform = int(BuildTarget.PSP2)
    output = bundle_file.save(packer='lz4')
    (_, written), index = objects(UnityPy.load(output), 30)
    if not (same_geometry(signature(index), original) if entry.get('tight_sprites') else signature(index) == original):
        raise ValueError('Texture or Sprite layout changed during conversion')
    expected = {name: entry[name] for name in ('name', 'bundle_name', 'container', 'preload', 'main')}
    if address_recipe(written, index) != expected:
        raise ValueError('Asset address verification failed')
    counts = Counter(kind for kind, _ in index)
    if counts['Texture2D'] != len(entry['textures']):
        raise ValueError('Texture count differs from the recipe')
    return output


def prepare_graphics(streaming, output, recipe_path, progress, cancel, *, excluded=()):
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    converter = convert
    phase = 'PSP2 graphics'
    if recipe['format'] == 'pwaat-portable-atlas/1':
        from . import atlas
        converter = atlas.convert
        phase = 'PSP2 atlases'
    elif recipe['format'] != 'pwaat-portable-graphics/1':
        raise PatchError('Unsupported graphics recipe.')
    files = [entry for entry in recipe['files'] if entry['path'].lower() not in excluded]
    for index, entry in enumerate(files, 1):
        check_cancel(cancel)
        relative = entry['path']
        source = streaming / relative
        target = output / 'Media/StreamingAssets' / relative
        if not source.resolve().is_relative_to(streaming) or not target.resolve().is_relative_to(output):
            raise PatchError('Bundle path points outside the selected folder.')
        try:
            if target.is_file() and sha256(target) == entry['output_sha256']:
                if progress:
                    progress(phase, index, len(files), relative)
                continue
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Unsupported file version')
            encoded = converter(raw, entry)
            if hashlib.sha256(encoded).hexdigest() != entry['output_sha256']:
                raise ValueError('Output does not match the verified conversion')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if sha256(target) != hashlib.sha256(encoded).hexdigest():
                    raise ValueError('Unexpected output file')
            else:
                temp = target.with_name(target.name + '.tmp')
                try:
                    temp.write_bytes(encoded)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError) as error:
            raise PatchError(f'Graphics bundle {relative}: {error}') from error
        if progress:
            progress(phase, index, len(files), relative)
    return {row['path'].lower() for row in files}
