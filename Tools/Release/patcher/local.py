"""Local conversion preparation. Completion is gated on all required stages."""
from pathlib import Path
import json
import os
import sys
import tempfile
from .core import PatchError, check_cancel, sha256
from .audio import convert_tree
from .graphics import prepare_graphics


def locate(folder):
    folder = Path(folder).resolve(strict=True)
    candidates = [folder, folder / 'PWAAT_Data']
    if folder.name.lower() == 'streamingassets':
        candidates.append(folder.parent)
    for p in candidates:
        if (p / 'StreamingAssets/Sound').is_dir() and (p / 'sharedassets0.assets').is_file():
            return p
    raise PatchError('Select the PC game folder or its PWAAT_Data folder.')


def recipe_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'recipes'


def check_original(game):
    game = locate(game)
    profile = json.loads((recipe_root() / 'steam-source.json').read_text(encoding='utf-8'))
    if profile['format'] != 'pwaat-steam-source/1':
        raise PatchError('Unsupported Steam source recipe.')
    for entry in profile['files']:
        source = game / entry['path']
        if not source.resolve().is_relative_to(game) or not source.is_file() or sha256(source) != entry['sha256']:
            raise PatchError('PC file does not match the supported clean Steam version: ' + entry['path'])
    return game


def reference_layout():
    profile = json.loads((recipe_root() / 'media-layout.json').read_text(encoding='utf-8'))
    if profile['format'] != 'pwaat-media-layout/1':
        raise PatchError('Unsupported Vita Media layout.')
    return profile


def check_reference_media(media, *, existing_output=False):
    profile = reference_layout()
    for entry in profile['core']:
        source = media / entry['path']
        if existing_output and not source.exists():
            continue
        if not source.resolve().is_relative_to(media) or not source.is_file() or sha256(source) != entry['sha256']:
            if existing_output:
                raise PatchError(f"The output contains a file from a different build: {entry['path']}. "
                                 f"Choose a new folder for {profile['reference']}.")
            raise PatchError(f"Installed Vita Media does not match {profile['reference']}: {entry['path']}")
    return profile


def complete_from_installed(media, output, report, progress=None, cancel=None):
    profile = check_reference_media(media)
    target_root = output / 'Media'
    check_reference_media(target_root, existing_output=True)
    missing = report['missing_reference_files']
    for relative in missing:
        source = media / relative
        target = target_root / relative
        if (not source.resolve().is_relative_to(media) or not source.is_file()
                or not target.resolve().is_relative_to(target_root) or target.exists()):
            raise PatchError('Could not copy a file from the installed Vita Media: ' + relative)
    for index, relative in enumerate(missing, 1):
        check_cancel(cancel)
        source = media / relative
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_path = tempfile.mkstemp(dir=target.parent)
        temporary = Path(temporary_path)
        try:
            with os.fdopen(handle, 'wb') as destination, source.open('rb') as origin:
                while data := origin.read(1024 * 1024):
                    check_cancel(cancel)
                    destination.write(data)
            if sha256(temporary, cancel) != sha256(source, cancel):
                raise PatchError('Source file changed: ' + relative)
            temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
        if progress:
            progress('Files from existing Vita Media', index, len(missing), relative)
    required = {relative.lower() for relative in profile['files']}
    actual = set()
    for file in target_root.rglob('*'):
        if file.is_file():
            relative = file.relative_to(target_root)
            key = relative.as_posix().lower()
            if not key.startswith(('streamingassets/russian/', 'streamingassets/russianvoice/')):
                actual.add(key)
    still_missing, extra = sorted(required - actual), sorted(actual - required)
    report.update(complete_media=not still_missing and not extra, missing_reference_files=still_missing,
                  extra_reference_files=extra,
                  copied_from_installed_media=len(missing), uses_existing_vita_media=True,
                  message=f"Created Media for {profile['reference']} using files from the local PS Vita installation.")
    if not report['complete_media']:
        raise PatchError('Media did not pass the file inventory check.')
    check_reference_media(target_root)
    (output / 'conversion-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def complete_from_delta(package_path, game, output, report, progress, cancel):
    from .core import Package, apply
    from .tools import tool_path

    profile = reference_layout()
    required = {name.lower() for name in profile['files']}
    missing = {name.lower() for name in report['missing_reference_files']}
    target = output / 'Media'
    check_reference_media(target, existing_output=True)
    package = Package(package_path)
    try:
        manifest = package.manifest
        covered = {entry['path'].lower() for entry in manifest['entries']}
        if manifest.get('reference') != profile['reference'] or not missing <= covered <= required:
            raise PatchError('The delta does not cover the missing files for this Media layout.')
    finally:
        package.close()
    result = apply(package_path, game, target, tool_path(), progress, cancel, overlay=True)
    actual = {path.relative_to(target).as_posix().lower() for path in target.rglob('*') if path.is_file()}
    if actual != required:
        raise PatchError('The patched Media did not pass the file inventory check.')
    check_reference_media(target)
    report.update(complete_media=True, missing_reference_files=[], extra_reference_files=[],
                  patched_files=result['files'], uses_existing_vita_media=False, remaining_stages=[],
                  message=f"Complete Media created for {profile['reference']} from Steam and the local delta package.")
    (output / 'conversion-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def prepare_boot_config(game, output):
    source = game / 'boot.config'
    target = output / 'Media/boot.config'
    if not source.resolve().is_relative_to(game) or not target.resolve().is_relative_to(output):
        raise PatchError('boot.config path points outside the selected folder.')
    # The Steam single-instance option is Windows-specific. Only preserve the
    # debugger setting understood by the target Unity player.
    debugger = b'wait-for-native-debugger=0'
    if source.read_bytes().splitlines().count(debugger) != 1:
        raise PatchError('Unexpected Steam boot.config format.')
    expected = debugger + b'\n'
    if target.exists():
        if target.read_bytes() != expected:
            raise PatchError('The output folder contains boot.config from a different build.')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(expected)
            stream.close()
            staged.rename(target)
        finally:
            stream.close()
            staged.unlink(missing_ok=True)


def prepare(game, output, progress=None, cancel=None, *, ffmpeg=None, steam_only=False, graphics_exclusions=()):
    game, output = locate(game), Path(output).resolve()
    if output == game or game in output.parents or output in game.parents:
        raise PatchError('Choose a separate output folder outside the source game.')
    streaming = game / 'StreamingAssets'
    recipe = json.loads((recipe_root() / 'unchanged.json').read_text(encoding='utf-8'))
    if recipe['format'] != 'pwaat-local-copy/1':
        raise PatchError('Unsupported conversion recipe.')
    # Check all copy inputs before spending time encoding audio or creating output.
    sources = []
    for entry in recipe['files']:
        p = streaming / entry['path']
        if not p.resolve().is_relative_to(streaming) or not p.is_file() or sha256(p) != entry['sha256']:
            raise PatchError('Unsupported file version: ' + entry['path'])
        sources.append((p, entry))
        check_cancel(cancel)
    result = convert_tree(game, output, progress, cancel)
    for index, (source, entry) in enumerate(sources):
        check_cancel(cancel)
        target = output / 'Media/StreamingAssets' / entry['path']
        if not target.resolve().is_relative_to(output):
            raise PatchError('Output path points outside the selected folder.')
        if target.exists() and sha256(target) != entry['sha256']:
            raise PatchError('Unexpected output file: ' + entry['path'])
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    data = source.read_bytes()
                    import hashlib
                    if hashlib.sha256(data).hexdigest() != entry['sha256']:
                        raise PatchError('Source file changed: ' + entry['path'])
                    stream.write(data)
                os.replace(name, target)
            finally:
                Path(name).unlink(missing_ok=True)
        if progress:
            progress('Scenario data', index + 1, len(sources), entry['path'])
    processed = {e['path'].lower() for _, e in sources}
    graphics_recipe = recipe_root() / 'portable-graphics.json'
    graphics = prepare_graphics(streaming, output, graphics_recipe, progress, cancel, excluded=graphics_exclusions) if graphics_recipe.is_file() else set()
    processed.update(graphics)
    additional_graphics = {}
    if steam_only:
        for stage, filename in (('tight_graphics', 'portable-tight-graphics.json'), ('atlases', 'portable-atlases.json')):
            profile = recipe_root() / filename
            if profile.is_file():
                names = prepare_graphics(streaming, output, profile, progress, cancel, excluded=graphics_exclusions)
                processed.update(names)
                additional_graphics[stage] = len(names)
    data_recipe = recipe_root() / 'portable-script-data.json'
    script_data = set()
    if steam_only and data_recipe.is_file():
        from .portable_data import prepare_data_bundles
        script_data = prepare_data_bundles(streaming, output, data_recipe, progress, cancel)
    processed.update(script_data)
    menu_text = set()
    menu_recipe = recipe_root() / 'stock-menu.json'
    if steam_only and menu_recipe.is_file():
        from .menu_text import prepare as prepare_menu_text
        menu_text = prepare_menu_text(streaming, output, menu_recipe, progress, cancel)
        processed.update(menu_text)
    movies = {}
    if ffmpeg:
        from .movie_frames import prepare_stock_movies
        movies = prepare_stock_movies(streaming, output, ffmpeg, progress, cancel)
    if steam_only:
        prepare_boot_config(game, output)
    missing = sorted(p.relative_to(streaming).as_posix() for p in streaming.rglob('*')
                     if p.is_file() and p.suffix != '.meta' and
                     p.relative_to(streaming).as_posix().lower() not in processed and
                     p.relative_to(streaming).parts[0].lower() != 'sound')
    reference = reference_layout()
    required = reference['files']
    missing_reference = [path for path in required if not (output / 'Media' / path).is_file()]
    report = {'complete_media': False, 'audio': result, 'copied': len(sources), 'portable_graphics': len(graphics),
              'additional_graphics': additional_graphics,
              'movies_from_steam': movies,
              'portable_script_data': len(script_data),
              'stock_menu_tables': len(menu_text),
              'reference_build': reference['reference'], 'reference_file_count': len(required),
              'missing_reference_files': missing_reference,
              'remaining_streaming_files': missing,
              'remaining_stages': ['graphics/sprites/animations', 'meshes/shaders', 'movies', 'core Media'],
              'message': 'Partial Media created. More files are required to run the game.'}
    (output / 'conversion-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
