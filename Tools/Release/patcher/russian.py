"""Build a Failing Forward overlay from the user's PC and installed Vita copies."""

import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile

from .core import PatchError, check_cancel, safe_relative, sha256
from .local import locate, recipe_root


def installed_media(folder):
    folder = Path(folder).resolve(strict=True)
    media = folder / 'Media' if (folder / 'Media').is_dir() else folder
    if any(not (media / name).is_file() for name in ('globalgamemanagers', 'resources.assets')):
        raise PatchError('Select the Media folder of an existing PS Vita installation.')
    if not (media / 'StreamingAssets').is_dir() or not (media / 'Modules').is_dir():
        raise PatchError('The selected Vita Media is missing StreamingAssets or Modules.')
    return media


def publish_overlay(staging, destination, files, cancel):
    try:
        os.rename(staging, destination)
        return
    except PermissionError:
        # Windows can deny moving the staging directory while a scanner has a
        # handle open. Publish the manifest last so a partial overlay stays inactive.
        destination.mkdir()

    created = []
    directories = {destination}
    try:
        for relative in [*files, 'files.txt']:
            check_cancel(cancel)
            target = destination / relative
            parent = target.parent
            while parent != destination:
                directories.add(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                created.append(target)
                stream.write((staging / relative).read_bytes())
    except BaseException:
        for file in reversed(created):
            file.unlink(missing_ok=True)
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            if directory.is_dir():
                directory.rmdir()
        raise


def prepare_russian(game, installed, output, progress=None, cancel=None):
    pc = locate(game)
    native = installed_media(installed)
    output = Path(output).resolve()
    for source in (pc, native):
        if output == source or source in output.parents or output in source.parents:
            raise PatchError('Choose a separate output folder outside the source installations.')

    manifest = json.loads((recipe_root() / 'failing-forward.json').read_text(encoding='utf-8'))
    if manifest['format'] != 'pwaat-failing-forward/1':
        raise PatchError('Unsupported Failing Forward recipe.')
    streaming = pc / 'StreamingAssets'
    templates = native / 'StreamingAssets'
    destination = output / 'Media/StreamingAssets/Russian'
    if not destination.resolve().is_relative_to(output) or destination.exists():
        raise PatchError('The output already contains a Russian folder. Choose a different output folder.')

    localization = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[3])) / 'Tools/Localization'
    sys.path.insert(0, str(localization))
    try:
        from convert_failing_forward import NEW, convert_bundle
        from stage_text import adapt_text
    finally:
        sys.path.remove(str(localization))

    files = manifest['files']
    for entry in files:
        check_cancel(cancel)
        relative = safe_relative(entry['path'])
        source = streaming / relative
        if not source.resolve().is_relative_to(streaming) or not source.is_file() or sha256(source) != entry['sha256']:
            raise PatchError('File does not match the supported Failing Forward version: ' + relative)
        if relative.endswith('.unity3d'):
            template = templates / ('GS1/BG/bg00bu.unity3d' if relative in NEW else relative)
            if not template.resolve().is_relative_to(templates) or not template.is_file():
                raise PatchError('Bundle missing from the installed Vita Media: ' + relative)

    destination.parent.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory(prefix='.russian-', dir=destination.parent) as temp:
        staging = Path(temp) / 'Russian'
        for index, entry in enumerate(files, 1):
            check_cancel(cancel)
            relative = entry['path']
            source = streaming / relative
            try:
                raw = source.read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise PatchError('Source file changed: ' + relative)
                if relative.endswith('.unity3d'):
                    raw, _ = convert_bundle(relative, raw, None, templates, entry['animation_fields'])
                else:
                    raw, _ = adapt_text(relative, raw)
            except Exception as error:
                raise PatchError(f'Could not convert {relative}: {error}') from error
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            written.append(relative)
            if progress:
                progress('Failing Forward', index, len(files), relative)
        (staging / 'files.txt').write_text('\n'.join(sorted(written, key=str.lower)) + '\n', encoding='utf-8')
        check_cancel(cancel)
        publish_overlay(staging, destination, written, cancel)
    return {'installed_text_and_graphics': True, 'files': len(written), 'voice_included': False,
            'message': 'Russian text and graphics are ready in Media/StreamingAssets/Russian.'}
