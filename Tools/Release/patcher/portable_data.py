"""Retarget self-contained script data after matching its contents against PSP2."""

import hashlib
import json
from pathlib import Path
import tempfile

from UnityPy.enums import BuildTarget

from .core import PatchError, check_cancel, safe_relative, sha256
from .graphics import address_recipe, apply_addresses, read_bundle


DATA_TYPES = {'MonoBehaviour', 'MonoScript', 'TextAsset'}
ADDRESS_FIELDS = ('name', 'bundle_name', 'container', 'preload', 'main')


def inspect_bundle(data, platform):
    env = read_bundle(data)
    if (len(env.assets) != 1 or len(env.file.files) != 1
            or int(env.assets[0].target_platform) != platform or env.assets[0].externals):
        raise ValueError('Bundle is not a standalone serialized file')
    index = {}
    bundle = None
    for obj in env.objects:
        kind = obj.type.name
        if kind == 'AssetBundle':
            tree = obj.read_typetree()
            if bundle or tree['m_Dependencies'] or tree['m_IsStreamedSceneAssetBundle']:
                raise ValueError('Bundle has dependencies or scene data')
            bundle = (obj, tree)
        elif kind in DATA_TYPES:
            tree = obj.read_typetree()
            key = (kind, tree['m_Name'])
            if key in index:
                raise ValueError('Ambiguous data asset name')
            index[key] = (obj, tree)
        else:
            raise ValueError('Unsupported asset class: ' + kind)
    if bundle is None or not index:
        raise ValueError('No script data')
    return env, bundle, index


def signature(index):
    names = {obj.path_id: key for key, (obj, _) in index.items()}

    def normalize(value):
        if isinstance(value, dict):
            if value.keys() == {'m_FileID', 'm_PathID'}:
                if value['m_FileID']:
                    raise ValueError('External script data pointer')
                return names[value['m_PathID']] if value['m_PathID'] else None
            return {field: normalize(item) for field, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return {name: normalize(tree) for name, (_, tree) in index.items()}


def convert(data, entry):
    env, (bundle, metadata), index = inspect_bundle(data, 19)
    expected = signature(index)
    apply_addresses(bundle, metadata, index, entry)
    asset = env.assets[0]
    old_name = asset.name
    asset.name = 'CAB-' + hashlib.sha256(('portable-data/' + entry['path'].lower()).encode()).hexdigest()[:32]
    env.file.files[asset.name] = env.file.files.pop(old_name)
    asset.target_platform = BuildTarget.PSP2
    asset._m_target_platform = int(BuildTarget.PSP2)
    output = env.file.save(packer='lz4')
    _, (_, written), result = inspect_bundle(output, 30)
    if signature(result) != expected or address_recipe(written, result) != {key: entry[key] for key in ADDRESS_FIELDS}:
        raise ValueError('Output data or load addresses changed')
    return output


def build_entry(relative, source, target):
    _, _, pc = inspect_bundle(source, 19)
    _, (_, metadata), vita = inspect_bundle(target, 30)
    if signature(pc) != signature(vita):
        raise ValueError('PC data differs from the PSP2 reference')
    entry = {'path': relative, 'sha256': hashlib.sha256(source).hexdigest(), **address_recipe(metadata, vita)}
    entry['output_sha256'] = hashlib.sha256(convert(source, entry)).hexdigest()
    return entry


def prepare_data_bundles(streaming, output, recipe, progress, cancel):
    profile = json.loads(recipe.read_text(encoding='utf-8'))
    if profile['format'] != 'pwaat-portable-script-data/1':
        raise PatchError('Unsupported data bundle recipe.')
    entries = profile['files']
    processed = set()
    for index, entry in enumerate(entries, 1):
        check_cancel(cancel)
        relative = safe_relative(entry['path'])
        source = streaming / relative
        target = output / 'Media/StreamingAssets' / relative
        if not source.resolve().is_relative_to(streaming) or not target.resolve().is_relative_to(output):
            raise PatchError('Bundle path points outside the selected folder.')
        try:
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Unsupported Steam bundle version')
            if target.exists():
                if sha256(target, cancel) != entry['output_sha256']:
                    raise ValueError('A different output bundle already exists')
            else:
                encoded = convert(raw, entry)
                if hashlib.sha256(encoded).hexdigest() != entry['output_sha256']:
                    raise ValueError('Output does not match the verified conversion')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    try:
                        temporary.write(encoded)
                        temporary.close()
                        temporary_path.rename(target)
                    finally:
                        temporary.close()
                        temporary_path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError) as error:
            raise PatchError(f'Data bundle {relative}: {error}') from error
        processed.add(relative.lower())
        if progress:
            progress('PSP2 data bundles', index, len(entries), relative)
    return processed
