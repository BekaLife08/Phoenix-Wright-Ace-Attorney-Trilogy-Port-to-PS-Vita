"""Private VCDIFF packages made from groups of verified local files."""

import hashlib
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

from .core import PatchError, inside, run_xdelta, sha256


FORMAT = 'pwaat-private-group-delta/1'
GROUP_LIMIT = 48 * 1024 * 1024


def concatenate(root, files, destination):
    with destination.open('xb') as stream:
        for name, size, digest in files:
            path = inside(root, name)
            if path.stat().st_size != size:
                raise PatchError('Wrong or changed input file: ' + name)
            hasher = hashlib.sha256()
            with path.open('rb') as original:
                while chunk := original.read(1024 * 1024):
                    hasher.update(chunk)
                    stream.write(chunk)
            if hasher.hexdigest() != digest:
                raise PatchError('Wrong or changed input file: ' + name)


def create(source, reference, expected, destination, tool, progress):
    source, reference, destination = Path(source).resolve(), Path(reference).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source) or destination.is_relative_to(reference):
        raise PatchError('Choose a new package path outside both inputs.')
    groups, copies = [], []
    inputs, targets = [], []
    source_bytes = target_bytes = 0
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        work = Path(folder)
        partial = work / 'package'
        def input_record(item):
            name, digest = item
            size = inside(reference, name).stat().st_size
            original = inside(source, name)
            if original.is_file():
                return name, digest, size, original.stat().st_size, sha256(original)
            return name, digest, size, 0, None

        with (
            ZipFile(partial, 'x') as archive,
            ThreadPoolExecutor(max_workers=4) as pool,
            ThreadPoolExecutor(max_workers=8) as scanner,
        ):
            pending = deque()

            def encode(label, source_files, target_files):
                old, new, delta, verified = (work / f'{label}-{name}' for name in ('old', 'new', 'delta', 'verified'))
                if source_files:
                    concatenate(source, source_files, old)
                concatenate(reference, target_files, new)
                options = ['-s', old] if source_files else []
                run_xdelta(tool, ['-A', '-e', '-1', '-S', 'none', *options, new, delta])
                run_xdelta(tool, ['-d', *options, delta, verified])
                if sha256(new) != sha256(verified):
                    raise PatchError('VCDIFF round trip failed: ' + label)
                for path in (old, new, verified):
                    path.unlink(missing_ok=True)
                return delta, {'blob': label, 'sha256': sha256(delta), 'sources': source_files, 'targets': target_files}

            def write_group():
                delta, group = pending.popleft().result()
                label = group['blob']
                archive.write(delta, label)
                groups.append(group)
                delta.unlink()
                progress('Encoded and verified groups', len(groups), 0, label)

            def finish():
                label = f'group-{len(groups) + len(pending):03d}'
                pending.append(pool.submit(encode, label, inputs[:], targets[:]))
                inputs.clear()
                targets.clear()
                if len(pending) >= 6:
                    write_group()

            for name, digest, size, original_size, original_hash in scanner.map(input_record, expected.items()):
                if original_hash == digest:
                    copies.append((name, size, digest))
                    continue
                if targets and max(source_bytes + original_size, target_bytes + size) > GROUP_LIMIT:
                    finish()
                    source_bytes = target_bytes = 0
                if original_hash:
                    inputs.append((name, original_size, original_hash))
                targets.append((name, size, digest))
                source_bytes += original_size
                target_bytes += size
            if targets:
                finish()
            while pending:
                write_group()
            archive.writestr('manifest.json', json.dumps({
                'format': FORMAT, 'reference': 'build23', 'private_game_derived': True,
                'copied': copies, 'groups': groups,
            }, separators=(',', ':')))
        partial.rename(destination)
    return len(groups), len(copies)


def apply_package(package, source, target, expected, tool, progress):
    target.mkdir(parents=True, exist_ok=True)
    with ZipFile(package) as archive, tempfile.TemporaryDirectory(dir=target.parent) as folder:
        info = archive.getinfo('manifest.json')
        if info.file_size > 32 * 1024 * 1024:
            raise PatchError('The patch manifest is too large.')
        manifest = json.loads(archive.read(info))
        if manifest.get('format') != FORMAT or manifest.get('reference') != 'build23':
            raise PatchError('Select the private VCDIFF package for build23.')
        groups, copies = manifest['groups'], manifest['copied']
        rows = copies + [row for group in groups for row in group['targets']]
        if len(rows) != len(expected) or {name: digest for name, _, digest in rows} != expected:
            raise PatchError('The package does not match the complete build23 inventory.')
        names = ['manifest.json'] + [f'group-{index:03d}' for index in range(len(groups))]
        if sorted(archive.namelist()) != sorted(names) or [g['blob'] for g in groups] != names[1:]:
            raise PatchError('Unexpected package contents.')
        work = Path(folder)
        for index, (name, size, digest) in enumerate(copies, 1):
            original = inside(source, name)
            destination = inside(target, name)
            if not original.is_file() or original.stat().st_size != size or sha256(original) != digest:
                raise PatchError('Wrong or missing Steam input: ' + name)
            if not destination.is_file():
                staged = work / 'file'
                shutil.copyfile(original, staged)
                if sha256(staged) != digest:
                    raise PatchError('The Steam input changed while copying: ' + name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(destination)
            elif sha256(destination) != digest:
                raise PatchError('The output contains a different file: ' + name)
            progress('Copying compatible Steam files', index, len(copies), name)
        def unpack(group):
            members = group['targets']
            if all((path := inside(target, name)).is_file() and sha256(path) == digest
                   for name, _, digest in members):
                return group['blob']
            with tempfile.TemporaryDirectory(dir=work) as scratch:
                directory = Path(scratch)
                old, delta, new = (directory / name for name in ('old', 'delta', 'new'))
                if group['sources']:
                    concatenate(source, group['sources'], old)
                with archive.open(group['blob']) as file, delta.open('xb') as staged:
                    shutil.copyfileobj(file, staged, 1024 * 1024)
                if sha256(delta) != group['sha256']:
                    raise PatchError('Corrupt VCDIFF group: ' + group['blob'])
                options = ['-s', old] if group['sources'] else []
                run_xdelta(tool, ['-d', *options, delta, new])
                if new.stat().st_size != sum(size for _, size, _ in members):
                    raise PatchError('Wrong reconstructed group size: ' + group['blob'])
                with new.open('rb') as stream:
                    for name, size, digest in members:
                        destination = inside(target, name)
                        staged = directory / 'file'
                        with staged.open('xb') as file:
                            remaining = size
                            while remaining:
                                data = stream.read(min(remaining, 1024 * 1024))
                                if not data:
                                    raise PatchError('Truncated output in VCDIFF group: ' + name)
                                file.write(data)
                                remaining -= len(data)
                        if sha256(staged) != digest:
                            raise PatchError('Wrong output fingerprint: ' + name)
                        if destination.exists():
                            if not destination.is_file() or sha256(destination) != digest:
                                raise PatchError('The output contains a different file: ' + name)
                            staged.unlink()
                        else:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            staged.replace(destination)
                    if stream.read(1):
                        raise PatchError('Unexpected trailing bytes in VCDIFF group.')
            return group['blob']

        with ThreadPoolExecutor(max_workers=4) as pool:
            for index, label in enumerate(pool.map(unpack, groups), 1):
                progress('Applying VCDIFF group', index, len(groups), label)
