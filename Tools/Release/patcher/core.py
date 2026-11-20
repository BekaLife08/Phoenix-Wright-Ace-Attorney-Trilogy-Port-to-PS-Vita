"""Apply a versioned patch using local game data, without running the game.

Binary-delta packages are explicitly game-derived data, not certified asset-free
conversion recipes. Package authoring and publication are separate operations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile

FORMAT = 'pwaat-vita-patch/1'
MARKER = '.pwaat-patch-state.json'
MAX_MANIFEST = 32 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024 * 1024
HEX = re.compile(r'^[0-9a-f]{64}$')


class PatchError(Exception):
    pass


class Cancelled(PatchError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('Operation canceled. Run the converter again to resume.')


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise PatchError('Invalid package path: ' + repr(value))
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise PatchError('Invalid package path: ' + repr(value))
    if any(p.endswith((' ', '.')) for p in path.parts):
        raise PatchError('Ambiguous Windows path: ' + value)
    if any(ord(c) < 32 or c in '<>"|?*' for c in value):
        raise PatchError('Invalid path characters: ' + value)
    for part in path.parts:
        if re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', part, re.I):
            raise PatchError('Reserved filename: ' + value)
    if path.suffix.lower() in ('.exe', '.bat', '.cmd', '.ps1', '.lnk', '.vbs'):
        raise PatchError('Windows executables are not allowed in Media: ' + value)
    return value


def inside(root: Path, relative: str):
    result = root.joinpath(*safe_relative(relative).split('/'))
    if not result.resolve().is_relative_to(root.resolve()):
        raise PatchError('Path points outside the selected folder: ' + relative)
    return result


def sha256(path: Path, cancel=None):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(chunk)
    return digest.hexdigest()


def data_root(folder: Path):
    folder = folder.expanduser().resolve(strict=True)
    candidates = [folder, folder / 'PWAAT_Data']
    if folder.name.lower() == 'streamingassets':
        candidates.append(folder.parent)
    for candidate in candidates:
        if (candidate / 'StreamingAssets').is_dir() and (candidate / 'sharedassets0.assets').is_file():
            return candidate.resolve()
    raise PatchError('Выберите папку ПК-игры, PWAAT_Data или её StreamingAssets.')


def _hash_record(record):
    return (isinstance(record, dict) and isinstance(record.get('bytes'), int)
            and 0 <= record['bytes'] <= MAX_FILE and HEX.fullmatch(record.get('sha256', '')))


class Package:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.zip = zipfile.ZipFile(self.path)
        try:
            names = self.zip.namelist()
            if len(names) != len(set(names)):
                raise PatchError('Повторяющиеся записи в пакете.')
            info = self.zip.getinfo('manifest.json')
            if info.file_size > MAX_MANIFEST:
                raise PatchError('Слишком большой манифест.')
            raw = self.zip.read(info)
            self.identity = hashlib.sha256(raw).hexdigest()
            self.manifest = json.loads(raw)
            self._validate()
        except PatchError:
            self.zip.close()
            raise
        except Exception as error:
            self.zip.close()
            raise PatchError('Повреждён или несовместим пакет патча: ' + str(error)) from error

    def _validate(self):
        m = self.manifest
        if m.get('format') != FORMAT or m.get('payload_policy') != 'binary-delta':
            raise PatchError('Неподдерживаемый формат пакета.')
        sources, entries = m.get('sources'), m.get('entries')
        if not isinstance(sources, dict) or not isinstance(entries, list) or not 1 <= len(entries) <= 20000:
            raise PatchError('Некорректный состав пакета.')
        seen = set()
        for name, record in sources.items():
            safe_relative(name)
            if not _hash_record(record) or name.casefold() in seen:
                raise PatchError('Некорректная запись исходного файла: ' + name)
            seen.add(name.casefold())
        seen = set()
        blobs = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise PatchError('Некорректная запись Media.')
            name = safe_relative(entry.get('path'))
            if name.casefold() in seen or name.split('/')[0].startswith('.pwaat') or not _hash_record(entry):
                raise PatchError('Некорректная запись Media: ' + name)
            seen.add(name.casefold())
            operation = entry.get('op')
            replacements = entry.get('replaces_sha256', [])
            if (not isinstance(replacements, list) or any(
                    not isinstance(digest, str) or not HEX.fullmatch(digest) for digest in replacements)):
                raise PatchError('Invalid replacement hashes: ' + name)
            if operation in ('copy', 'vcdiff') and entry.get('source') not in sources:
                raise PatchError('Не указан исходник для ' + name)
            if operation not in ('copy', 'vcdiff', 'runtime'):
                raise PatchError('Неизвестная операция для ' + name)
            if operation != 'copy':
                blob = entry.get('blob', {})
                if not _hash_record(blob):
                    raise PatchError('Некорректные данные патча: ' + name)
                blob_name = 'blobs/' + blob['sha256']
                if self.zip.getinfo(blob_name).file_size != blob['bytes']:
                    raise PatchError('Размер данных пакета не совпадает: ' + name)
                blobs.add(blob_name)
            if operation == 'runtime' and not (
                name.startswith(('Modules/', 'Plugins/', 'Metadata/'))
                or name in ('boot.config', 'SymbolMap', 'methods_pointedto_by_uievents.xml',
                            'platform_native_link.xml', 'preserved_derived_types.xml', 'UnityEngine.xml')
            ):
                raise PatchError('Готовые игровые данные запрещены в runtime-секции: ' + name)
        if set(self.zip.namelist()) != blobs | {'manifest.json'}:
            raise PatchError('В пакете есть посторонние файлы.')
        for name in seen:
            if any('/'.join(name.split('/')[:i]) in seen for i in range(1, len(name.split('/')))):
                raise PatchError('Конфликт файла и папки в пакете: ' + name)

    def close(self):
        self.zip.close()


def run_xdelta(tool, arguments, cancel=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen([str(tool), *map(str, arguments)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=flags)
    try:
        # xdelta emits only errors with these arguments. communicate avoids a
        # full pipe blocking the child when a corrupt patch produces diagnostics.
        while True:
            check_cancel(cancel)
            try:
                output, _ = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise PatchError('Не удалось применить дельту: ' + output.decode('utf-8', errors='replace')[-1500:])
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def _copy_stream(source, target, cancel):
    while chunk := source.read(1024 * 1024):
        check_cancel(cancel)
        target.write(chunk)


def _state(marker, identity, complete):
    temporary = marker.with_name(marker.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps({'package': identity, 'complete': complete}), encoding='utf-8')
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def apply(package_path, game, output, xdelta, progress=lambda *args: None, cancel=None, *, overlay=False):
    root, output = data_root(Path(game)), Path(output).expanduser().resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise PatchError('Папки исходной игры и результата должны быть раздельными.')
    if Path(game).resolve().name != 'StreamingAssets' and output.is_relative_to(Path(game).resolve()):
        raise PatchError('Создайте Media вне папки исходной игры.')
    if output.exists() and not output.is_dir():
        raise PatchError('Путь результата уже занят файлом.')
    package = Package(package_path)
    try:
        if package.path.is_relative_to(output):
            raise PatchError('Пакет патча должен находиться вне выходной Media.')
        manifest = package.manifest
        marker = output.parent / '.pwaat-media-delta-state.json' if overlay else output / MARKER
        if marker.is_symlink():
            raise PatchError('Файл состояния не должен быть символической ссылкой.')
        if not overlay and output.exists() and any(output.iterdir()):
            if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('package') != package.identity:
                raise PatchError('Папка результата занята. Выберите пустую папку или результат этого же патча для продолжения.')
        source_paths = {}
        total_sources = len(manifest['sources'])
        # Check all input files before creating output or applying any patch.
        for index, (name, expected) in enumerate(manifest['sources'].items(), 1):
            check_cancel(cancel)
            source = inside(root, name)
            if not source.is_file() or source.stat().st_size != expected['bytes'] or sha256(source, cancel) != expected['sha256']:
                raise PatchError('Несовместимый или изменённый файл ПК-игры: ' + name)
            source_paths[name] = source
            progress('Проверка файлов игры', index, total_sources, name)
        entries = manifest['entries']
        done = set()
        replacing = {}
        for entry in entries:
            destination = inside(output, entry['path'])
            existing = sha256(destination, cancel) if destination.is_file() and (
                destination.stat().st_size == entry['bytes'] or overlay and entry.get('replaces_sha256')
            ) else None
            if existing == entry['sha256']:
                done.add(entry['path'])
            elif overlay and destination.exists():
                if existing not in entry.get('replaces_sha256', []):
                    raise PatchError('The patch would replace a different output file: ' + entry['path'])
                replacing[entry['path']] = existing
        pending = [e for e in entries if e['path'] not in done]
        needed = sum(e['bytes'] for e in pending)
        probe = output
        while not probe.exists():
            probe = probe.parent
        scratch = max((e['bytes'] + e.get('blob', {}).get('bytes', 0) for e in pending), default=0)
        if shutil.disk_usage(probe).free < needed + scratch + 64 * 1024 * 1024:
            raise PatchError('Недостаточно свободного места для Media и временного файла.')
        output.mkdir(parents=True, exist_ok=True)
        _state(marker, package.identity, False)
        for index, entry in enumerate(entries, 1):
            check_cancel(cancel)
            destination = inside(output, entry['path'])
            phase = 'Applying delta' if overlay else 'Создание Media'
            if entry['path'] in done:
                progress(phase, index, len(entries), entry['path'])
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + '.pwaat-' + uuid.uuid4().hex)
            patch_temp = None
            try:
                if entry['op'] == 'copy':
                    with source_paths[entry['source']].open('rb') as src, temporary.open('xb') as dst:
                        _copy_stream(src, dst, cancel)
                else:
                    blob = entry['blob']
                    patch_temp = destination.with_name(destination.name + '.delta-' + uuid.uuid4().hex)
                    with package.zip.open('blobs/' + blob['sha256']) as src, patch_temp.open('xb') as dst:
                        _copy_stream(src, dst, cancel)
                    if sha256(patch_temp, cancel) != blob['sha256']:
                        raise PatchError('Повреждены данные патча: ' + entry['path'])
                    if entry['op'] == 'runtime':
                        os.replace(patch_temp, temporary)
                    else:
                        run_xdelta(xdelta, ['-d', '-s', source_paths[entry['source']], patch_temp, temporary], cancel)
                if temporary.stat().st_size != entry['bytes'] or sha256(temporary, cancel) != entry['sha256']:
                    raise PatchError('Результат не прошёл проверку: ' + entry['path'])
                if entry['path'] in replacing:
                    if not destination.is_file() or sha256(destination, cancel) != replacing[entry['path']]:
                        raise PatchError('The file changed before replacement: ' + entry['path'])
                    os.replace(temporary, destination)
                elif overlay and os.name == 'nt':
                    os.rename(temporary, destination)
                elif overlay:
                    # Publish without replacing another file, even if it appeared after preflight.
                    os.link(temporary, destination)
                else:
                    os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
                if patch_temp:
                    patch_temp.unlink(missing_ok=True)
            progress(phase, index, len(entries), entry['path'])
        _state(marker, package.identity, True)
        return {'files': len(entries), 'bytes': sum(e['bytes'] for e in entries), 'output': str(output)}
    finally:
        package.close()
