"""Apply the compact build24 core + IL2CPP xdelta package."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

from .core import PatchError, check_cancel, inside, run_xdelta, sha256
from .group_delta import concatenate


FORMAT = 'pwaat-build24-core/1'


def read_manifest(package_path):
    with ZipFile(package_path) as archive:
        info = archive.getinfo('manifest.json')
        if info.file_size > 16 * 1024 * 1024:
            raise PatchError('Oversized core patch manifest.')
        manifest = json.loads(archive.read(info))
    if manifest.get('format') != FORMAT or manifest.get('reference') != 'build24':
        raise PatchError('Select the build24 core patch file (build24-core.pwaatpatch).')
    return manifest


def package_targets(package_path):
    manifest = read_manifest(package_path)
    targets = {}
    for entry in manifest.get('direct', []):
        targets[entry['path']] = entry['sha256']
    for group in manifest.get('deltas', []):
        for name, _, digest in group['targets']:
            targets[name] = digest
    return targets


def apply_core_package(package_path, steam_data, media_root, tool, progress=None, cancel=None):
    manifest = read_manifest(package_path)
    media_root.mkdir(parents=True, exist_ok=True)
    direct = manifest.get('direct', [])
    deltas = manifest.get('deltas', [])
    total = len(direct) + len(deltas)
    done = 0

    with ZipFile(package_path) as archive, tempfile.TemporaryDirectory(prefix='.b24-core-', dir=media_root.parent) as folder:
        work = Path(folder)

        # 1. Pure engine & custom Vita UI files
        for entry in direct:
            check_cancel(cancel)
            done += 1
            rel = entry['path']
            digest = entry['sha256']
            dest = inside(media_root, rel)
            if dest.is_file() and sha256(dest, cancel) == digest:
                if progress:
                    progress('Core & Vita UI', done, total, rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = work / 'direct.tmp'
            with archive.open(entry['blob']) as src, staged.open('wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if sha256(staged, cancel) != digest:
                raise PatchError('Corrupt core file in patch: ' + rel)
            staged.replace(dest)
            if progress:
                progress('Core & Vita UI', done, total, rel)

        # 2. VCDIFF / xdelta groups reconstructed from verified Steam inputs
        for group in deltas:
            check_cancel(cancel)
            done += 1
            label = group['name']
            members = group['targets']
            if all((p := inside(media_root, name)).is_file() and sha256(p, cancel) == digest
                   for name, _, digest in members):
                if progress:
                    progress('Patching core & IL2CPP', done, total, label)
                continue
            with tempfile.TemporaryDirectory(prefix=label + '-', dir=work) as sub:
                sdir = Path(sub)
                old_file, delta_file, new_file = sdir / 'old', sdir / 'delta', sdir / 'new'
                if group['sources']:
                    concatenate(steam_data, group['sources'], old_file)
                with archive.open(group['blob']) as src, delta_file.open('wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                if sha256(delta_file, cancel) != group['sha256']:
                    raise PatchError('Corrupt xdelta group: ' + label)
                opts = ['-s', old_file] if group['sources'] else []
                run_xdelta(tool, ['-d', *opts, delta_file, new_file])
                expected_total = sum(size for _, size, _ in members)
                if new_file.stat().st_size != expected_total:
                    raise PatchError('Unexpected reconstructed size for group: ' + label)
                with new_file.open('rb') as stream:
                    for name, size, digest in members:
                        check_cancel(cancel)
                        dest = inside(media_root, name)
                        staged = sdir / 'member.tmp'
                        with staged.open('wb') as out:
                            remaining = size
                            while remaining:
                                chunk = stream.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    raise PatchError('Truncated xdelta output: ' + name)
                                out.write(chunk)
                                remaining -= len(chunk)
                        if sha256(staged, cancel) != digest:
                            raise PatchError('SHA-256 mismatch after xdelta: ' + name)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        staged.replace(dest)
                    if stream.read(1):
                        raise PatchError('Trailing bytes in xdelta group: ' + label)
            if progress:
                progress('Patching core & IL2CPP', done, total, label)


def apply_russian_voice(package_path, media_root, progress=None, cancel=None):
    manifest = read_manifest(package_path)
    entries = manifest.get('russian_voice', [])
    if not entries:
        return 0
    media_root.mkdir(parents=True, exist_ok=True)
    with ZipFile(package_path) as archive, tempfile.TemporaryDirectory(prefix='.b24-rv-', dir=media_root.parent) as folder:
        work = Path(folder)
        for index, entry in enumerate(entries, 1):
            check_cancel(cancel)
            rel = entry['path']
            digest = entry['sha256']
            dest = inside(media_root, rel)
            if dest.is_file() and sha256(dest, cancel) == digest:
                if progress:
                    progress('Russian voice pack', index, len(entries), rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = work / 'rv.tmp'
            with archive.open(entry['blob']) as src, staged.open('wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if sha256(staged, cancel) != digest:
                raise PatchError('Corrupt Russian voice file in patch: ' + rel)
            staged.replace(dest)
            if progress:
                progress('Russian voice pack', index, len(entries), rel)
    return len(entries)
