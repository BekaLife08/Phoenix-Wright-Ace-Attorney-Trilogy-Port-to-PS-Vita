"""Convert self-contained Steam animation atlases into PSP2 bundle layouts."""

import copy
import hashlib

from PIL import Image
from UnityPy.enums import BuildTarget, ClassIDType
from UnityPy.helpers.Tpk import get_typetree_node

from .graphics import address_recipe, apply_addresses, read_bundle
from .portable_data import signature


ATLAS_SIZE = 'vita_atlas_size'
TEXT_ASSET_HASH = bytes.fromhex('486ba4e15dbd6aea8ac1a064305889c8')
KINDS = {'MonoScript', 'MonoBehaviour', 'TextAsset', 'Texture2D'}


def inspect_atlas(data, platform):
    env = read_bundle(data)
    if (len(env.assets) != 1 or int(env.assets[0].target_platform) != platform or env.assets[0].externals
            or env.assets[0].header.version != 17 or not env.assets[0]._enable_type_tree):
        raise ValueError('Unsupported serialized atlas')
    index = {}
    bundle = None
    for obj in env.objects:
        kind = obj.type.name
        tree = obj.read_typetree()
        if kind == 'AssetBundle':
            if bundle or tree['m_Dependencies'] or tree['m_IsStreamedSceneAssetBundle']:
                raise ValueError('Atlas has external data')
            bundle = obj, tree
        elif kind in KINDS:
            key = kind, tree['m_Name']
            if key in index:
                raise ValueError('Duplicate atlas asset name')
            index[key] = obj, tree
        else:
            raise ValueError('Unexpected atlas asset class')
    textures = [key for key in index if key[0] == 'Texture2D']
    if bundle is None or len(textures) != 1 or not any(key[0] == 'MonoBehaviour' for key in index):
        raise ValueError('Expected one texture and animation data')
    return env, bundle, index, textures[0]


def tables(index):
    return signature({key: value for key, value in index.items() if key[0] != 'Texture2D'
                      and key != ('TextAsset', ATLAS_SIZE)})


def add_size(asset, prototype, dimensions):
    # TextAsset comes from Unity's class data. Its content is computed from Steam.
    if any(entry.class_id == ClassIDType.TextAsset for entry in asset.types):
        raise ValueError('Unexpected TextAsset type in Steam atlas')
    entry = copy.copy(prototype.serialized_type)
    entry.class_id = int(ClassIDType.TextAsset)
    root = get_typetree_node(entry.class_id, asset.version)
    nodes = root.to_dict_list()
    for node in nodes:
        node.pop('m_Children', None)
        node.setdefault('m_TypeFlags', int(node['m_Type'] == 'Array'))
    entry.node = type(root).from_list(nodes)
    entry.old_type_hash = TEXT_ASSET_HASH
    asset.types.append(entry)
    obj = copy.copy(prototype)
    obj.type_id = len(asset.types) - 1
    obj.serialized_type = entry
    obj.type = ClassIDType.TextAsset
    obj.class_id = int(obj.type)
    obj.path_id = next(number for number in range(1, len(asset.objects) + 2) if number not in asset.objects)
    data = {'m_Name': ATLAS_SIZE, 'm_Script': '{} {}'.format(*dimensions)}
    obj.save_typetree(data)
    asset.objects[obj.path_id] = obj
    return obj, data


def convert(data, entry):
    env, (bundle, metadata), index, texture_key = inspect_atlas(data, 19)
    original = tables(index)
    original_texture, _ = index.pop(texture_key)
    texture = original_texture.read()
    original_size = texture.m_Width, texture.m_Height
    settings = entry['texture']
    size = settings['width'], settings['height']
    if max(size) > 1024 or min(size) < 1 or texture.m_MipCount != 1 or settings['format'] not in (4, 10, 12):
        raise ValueError('Unsupported Vita atlas dimensions or encoding')
    if size == original_size and settings['format'] == texture.m_TextureFormat:
        texture.image_data = texture.get_image_data()
        texture.m_StreamData.path = ''
        texture.m_StreamData.size = 0
        texture.m_StreamData.offset = 0
    else:
        pixels = texture.image.resize(size, Image.Resampling.LANCZOS) if size != original_size else texture.image
        texture.set_image(pixels, target_format=settings['format'], mipmap_count=1)
    texture.m_Name = settings['name']
    texture.m_LightmapFormat = settings['lightmap_format']
    texture.save()
    index['Texture2D', settings['name']] = original_texture, original_texture.read_typetree()
    asset = env.assets[0]
    size_key = 'TextAsset', ATLAS_SIZE
    if entry['size_marker']:
        index[size_key] = add_size(asset, original_texture, original_size)
    apply_addresses(bundle, metadata, index, entry)
    asset.name = 'CAB-' + hashlib.sha256(('portable-atlas/' + entry['path'].lower()).encode()).hexdigest()[:32]
    env.file.files = {asset.name: asset}
    asset.target_platform = BuildTarget.PSP2
    asset._m_target_platform = int(BuildTarget.PSP2)
    output = env.file.save(packer='lz4')
    _, (_, written), result, result_texture = inspect_atlas(output, 30)
    tree = result[result_texture][1]
    if (tables(result) != original or (tree['m_Width'], tree['m_Height']) != size
            or tree['m_TextureFormat'] != settings['format'] or tree['m_StreamData']['size']
            or (entry['size_marker'] and result[size_key][1] != index[size_key][1])
            or address_recipe(written, result) != {field: entry[field] for field in
                                                  ('name', 'bundle_name', 'container', 'preload', 'main')}):
        raise ValueError('Converted atlas did not preserve its runtime layout')
    return output


def build_entry(relative, source, native):
    _, _, source_index, source_texture = inspect_atlas(source, 19)
    _, (_, metadata), target_index, target_texture = inspect_atlas(native, 30)
    if source_texture[1].casefold() != target_texture[1].casefold() or tables(source_index) != tables(target_index):
        raise ValueError('Animation tables or atlas names differ')
    source_tree = source_index[source_texture][1]
    tree = target_index[target_texture][1]
    marker = target_index.get(('TextAsset', ATLAS_SIZE))
    if marker:
        expected = '{} {}'.format(source_tree['m_Width'], source_tree['m_Height'])
        if marker[1]['m_Script'] != expected or marker[0].serialized_type.old_type_hash != TEXT_ASSET_HASH:
            raise ValueError('Unexpected atlas size marker')
    for field in ('m_MipCount', 'm_ImageCount', 'm_TextureDimension', 'm_ColorSpace', 'm_TextureSettings'):
        if source_tree[field] != tree[field]:
            raise ValueError('Vita atlas sampling or mip layout differs')
    return {'path': relative, 'sha256': hashlib.sha256(source).hexdigest(), 'size_marker': bool(marker),
            'texture': {'name': target_texture[1], 'width': tree['m_Width'], 'height': tree['m_Height'],
                        'format': tree['m_TextureFormat'], 'lightmap_format': tree['m_LightmapFormat']},
            **address_recipe(metadata, target_index)}
