"""Author the compact build24 core + IL2CPP xdelta patch and build24 layout."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from patcher.build24_core import FORMAT
from patcher.core import PatchError, inside, run_xdelta, sha256
from patcher.tools import tool_path


UNUSED_MOVIES = {
    'StreamingAssets/GS1/Movie/film00.unity3d',
    'StreamingAssets/GS1/Movie/film03.unity3d',
    'StreamingAssets/GS1/Movie/videonoise.unity3d',
    'StreamingAssets/GS1/moviescriptable/film01_confront.unity3d',
    'StreamingAssets/GS1/moviescriptable/film02_confront.unity3d',
}

DIRECT_ENGINE_FILES = [
    'Modules/pthread.suprx',
    'Plugins/PWAATrophyNative.suprx',
    'Plugins/PWAATrophyNative.suprx.b.suprx',
    'Plugins/PWAATrophyNative_stub.a',
    'Resources/mscorlib.dll-resources.dat',
    'Resources/System.dll-resources.dat',
    'Resources/unity default resources',
    'Resources/unity_builtin_extra',
    'SymbolMap',
    'UnityEngine.xml',
    'methods_pointedto_by_uievents.xml',
    'platform_native_link.xml',
    'preserved_derived_types.xml',
]

DIRECT_CUSTOM_VITA_UI = [
    'StreamingAssets/menu/common/court_record_03_xbox.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxb.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxf.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxg.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxk.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxl.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxs.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxt.unity3d',
    'StreamingAssets/menu/common/court_record_03_xboxu.unity3d',
    'StreamingAssets/menu/common/symbol_xbox.unity3d',
    'StreamingAssets/menu/option/option_list_bg_7.unity3d',
    'StreamingAssets/menu/option/option_list_bg_8.unity3d',
    'StreamingAssets/menu/option/option_list_bg_9.unity3d',
    'StreamingAssets/menu/option/symbol_xbox_large.unity3d',
]

IL2CPP_SOURCES = [
    'Managed/Assembly-CSharp.dll',
    'Managed/Assembly-CSharp-firstpass.dll',
]

IL2CPP_TARGETS = [
    'Modules/il2CppAssemblies.suprx',
    'Metadata/global-metadata.dat',
]

UNITY_CORE_FILES = [
    'globalgamemanagers',
    'globalgamemanagers.assets',
    'globalgamemanagers.assets.resS',
    'level0',
    'resources.assets',
    'resources.assets.resS',
    'sharedassets0.assets',
    'sharedassets0.assets.resS',
]

DELTA_EXTRA_STREAMING = [
    'StreamingAssets/menu/title/titleu.unity3d',
    'StreamingAssets/GS1/science/itm05a0.unity3d',
]


def _file_record(path, rel):
    return [rel, path.stat().st_size, sha256(path)]


def _read_target_bytes(rel, b24_media, b23_zip):
    p = b24_media / rel
    if p.is_file():
        return p.read_bytes()
    return b23_zip.read('Media/' + rel)


def _encode_group(tool, work, name, steam_root, source_rels, target_rels, b24_media, b23_zip):
    old_file = work / f'{name}.old'
    new_file = work / f'{name}.new'
    delta_file = work / f'{name}.vcdiff'
    verify_file = work / f'{name}.verify'

    src_records = []
    with old_file.open('wb') as out:
        for rel in source_rels:
            p = inside(steam_root, rel)
            if p.is_file():
                data = p.read_bytes()
                out.write(data)
                src_records.append([rel, len(data), hashlib.sha256(data).hexdigest()])

    tgt_records = []
    with new_file.open('wb') as out:
        for rel in target_rels:
            data = _read_target_bytes(rel, b24_media, b23_zip)
            out.write(data)
            tgt_records.append([rel, len(data), hashlib.sha256(data).hexdigest()])

    opts = ['-s', old_file] if src_records else []
    run_xdelta(tool, ['-A', '-e', '-9', *opts, new_file, delta_file])
    run_xdelta(tool, ['-d', *opts, delta_file, verify_file])
    if sha256(new_file) != sha256(verify_file):
        raise PatchError('xdelta round-trip verification failed for ' + name)

    raw_size = new_file.stat().st_size
    delta_size = delta_file.stat().st_size
    print(f'  Group {name:24s}: target={raw_size/1024/1024:6.2f} MB -> xdelta={delta_size/1024/1024:6.2f} MB ({len(tgt_records)} files)', flush=True)
    for tmp in (old_file, new_file, verify_file):
        tmp.unlink(missing_ok=True)
    return delta_file, {
        'name': name,
        'blob': f'deltas/{name}.vcdiff',
        'sha256': sha256(delta_file),
        'sources': src_records,
        'targets': tgt_records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steam', type=Path, required=True)
    parser.add_argument('--build24-media', type=Path, required=True)
    parser.add_argument('--build23-zip', type=Path, required=True)
    parser.add_argument('--build23-checksums', type=Path, required=True)
    parser.add_argument('--output-patch', type=Path, required=True)
    parser.add_argument('--output-layout', type=Path, required=True)
    args = parser.parse_args()

    tool = tool_path()
    args.output_patch.parent.mkdir(parents=True, exist_ok=True)
    if args.output_patch.exists():
        args.output_patch.unlink()

    direct_entries = []
    delta_groups = []
    updated_hashes = {}

    with ZipFile(args.build23_zip) as b23_zip, tempfile.TemporaryDirectory(prefix='b24-author-') as folder:
        work = Path(folder)
        partial = work / 'patch.zip'
        with ZipFile(partial, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
            # 1. Direct engine & custom Vita UI files
            direct_raw = 0
            for idx, rel in enumerate(DIRECT_ENGINE_FILES + DIRECT_CUSTOM_VITA_UI):
                data = _read_target_bytes(rel, args.build24_media, b23_zip)
                digest = hashlib.sha256(data).hexdigest()
                blob = f'direct/{idx:03d}.bin'
                archive.writestr(blob, data, compress_type=ZIP_DEFLATED, compresslevel=9)
                direct_entries.append({'path': rel, 'size': len(data), 'sha256': digest, 'blob': blob})
                updated_hashes[rel] = digest
                direct_raw += len(data)
            print(f'  Direct engine + Vita UI : raw={direct_raw/1024/1024:6.2f} MB ({len(direct_entries)} files)', flush=True)

            # 2. IL2CPP code xdelta against Steam Managed DLLs
            delta_file, group = _encode_group(
                tool, work, 'il2cpp-code', args.steam, IL2CPP_SOURCES, IL2CPP_TARGETS, args.build24_media, b23_zip
            )
            archive.write(delta_file, group['blob'], compress_type=ZIP_STORED)
            delta_groups.append(group)
            for name, _, digest in group['targets']:
                updated_hashes[name] = digest

            # 3. Unity core assets xdelta against Steam core assets
            delta_file, group = _encode_group(
                tool, work, 'unity-core-assets', args.steam, UNITY_CORE_FILES, UNITY_CORE_FILES, args.build24_media, b23_zip
            )
            archive.write(delta_file, group['blob'], compress_type=ZIP_STORED)
            delta_groups.append(group)
            for name, _, digest in group['targets']:
                updated_hashes[name] = digest

            # 4. Custom title logo + shader-bearing science bundle xdelta against Steam
            delta_file, group = _encode_group(
                tool, work, 'custom-title-and-shader', args.steam, DELTA_EXTRA_STREAMING, DELTA_EXTRA_STREAMING, args.build24_media, b23_zip
            )
            archive.write(delta_file, group['blob'], compress_type=ZIP_STORED)
            delta_groups.append(group)
            for name, _, digest in group['targets']:
                updated_hashes[name] = digest

            # 5. Optional fan-made Russian voice pack (installed only in russian/both modes)
            rv_entries = []
            rv_names = sorted(
                n for n in b23_zip.namelist()
                if n.startswith('Media/StreamingAssets/RussianVoice/') and not n.endswith('/')
            )
            for idx, zname in enumerate(rv_names):
                rel = zname[len('Media/'):]
                data = b23_zip.read(zname)
                digest = hashlib.sha256(data).hexdigest()
                blob = f'russian_voice/{idx:03d}.bin'
                archive.writestr(blob, data, compress_type=ZIP_DEFLATED, compresslevel=9)
                rv_entries.append({'path': rel, 'size': len(data), 'sha256': digest, 'blob': blob})
            print(f'  Fan Russian voice pack  : {len(rv_entries)} files', flush=True)

            manifest = {
                'format': FORMAT,
                'reference': 'build24',
                'direct': direct_entries,
                'deltas': delta_groups,
                'russian_voice': rv_entries,
            }
            archive.writestr('manifest.json', json.dumps(manifest, indent=2).encode('utf-8'), compress_type=ZIP_DEFLATED)

        partial.replace(args.output_patch)

    # Generate build24-layout.json (build23 base files minus 5 unused movies, with build24 core hashes)
    b23 = json.loads(args.build23_checksums.read_text(encoding='utf-8'))
    files = {}
    for name, digest in b23['files'].items():
        if name.casefold().startswith(('streamingassets/russian/', 'streamingassets/russianvoice/')):
            continue
        if name in UNUSED_MOVIES:
            continue
        files[name] = updated_hashes.get(name, digest)
    args.output_layout.write_text(json.dumps({'reference': 'build24', 'files': files}, indent=2) + '\n', encoding='utf-8')
    print(f'\nCreated {args.output_patch} ({args.output_patch.stat().st_size / 1024 / 1024:.2f} MB)')
    print(f'Created {args.output_layout} ({len(files)} base files, 5 unused movies removed)')


if __name__ == '__main__':
    main()
