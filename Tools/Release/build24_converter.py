"""PWAAT PS Vita (build24) local converter and core patcher."""

import argparse
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from threading import Event
from zipfile import BadZipFile

from build23_patcher import choose_directory, resource_root
from converter_console import progress, selected_ffmpeg
from patcher.build24_core import apply_core_package, apply_russian_voice, package_targets
from patcher.core import Cancelled, PatchError
from patcher.custom_language import install_custom_language, patch_font, patch_title_logo
from patcher.local import check_original, prepare
from patcher.movie_frames import prepare_confront_movies
from patcher.russian import installed_media, prepare_russian, publish_overlay
from patcher.streaming_ext import prepare_streaming_ext
from patcher.tools import tool_path


PACKAGE = 'build24-core.pwaatpatch'
OBSOLETE_MOVIES = (
    'StreamingAssets/GS1/Movie/film00.unity3d',
    'StreamingAssets/GS1/Movie/film03.unity3d',
    'StreamingAssets/GS1/Movie/videonoise.unity3d',
    'StreamingAssets/GS1/moviescriptable/film01_confront.unity3d',
    'StreamingAssets/GS1/moviescriptable/film02_confront.unity3d',
)


def prepare_output_location(source, output):
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise PatchError('Choose an output folder outside the Steam installation.')
    media = output / 'Media'
    if media.resolve() != media:
        raise PatchError('The output Media folder must be a regular directory.')
    if media.exists() and not media.is_dir():
        raise PatchError('The output Media path is occupied by a file.')
    for rel in OBSOLETE_MOVIES:
        stale = media / rel
        if stale.is_file():
            stale.unlink()
    return media


def layout_files():
    profile = json.loads((resource_root() / 'recipes/build24-layout.json').read_text(encoding='utf-8'))
    if profile.get('reference') != 'build24':
        raise PatchError('The layout recipe is for a different build.')
    return profile['files']


def assemble(args, cancelled):
    stock = check_original(args.steam)
    expected = layout_files()
    output = args.output.resolve()
    media = prepare_output_location(stock, output)
    package = args.patch.resolve(strict=True)
    if package.is_relative_to(output):
        raise PatchError('Keep the core patch file outside the output folder.')
    packaged = package_targets(package)
    if any(expected.get(name) != digest for name, digest in packaged.items()):
        raise PatchError('The core patch does not match the build24 layout.')
    if not (ffmpeg := selected_ffmpeg(args.ffmpeg)):
        raise PatchError('Keep ffmpeg.exe beside the converter or specify --ffmpeg.')

    excluded = {
        name.casefold().removeprefix('streamingassets/')
        for name in packaged
        if name.casefold().startswith('streamingassets/')
    }

    # 1. Standard local Steam conversions (audio, unchanged, graphics, atlases, data, menu text, 3 FFmpeg movies, boot.config)
    report = prepare(
        stock, output, progress, cancelled,
        ffmpeg=ffmpeg, steam_only=True, graphics_exclusions=excluded,
        layout={'reference': 'build24', 'files': expected},
    )

    # 2. Extended local Steam conversions (remaining 316 StreamingAssets: Gallery, BG, Atlases, 3D Science, Dictionaries)
    ext_recipe = resource_root() / 'recipes/portable-streaming-ext.json'
    ext_done = prepare_streaming_ext(
        stock / 'StreamingAssets', output, ext_recipe, progress, cancelled, excluded=excluded
    )
    report['extended_streaming'] = len(ext_done)

    # 3. Case-5 confront movies (film01_confront.frames.bin, film02_confront.frames.bin)
    confront_recipe = json.loads((resource_root() / 'recipes/build23-confront.json').read_text(encoding='utf-8'))
    report['movies_from_steam'].update(
        prepare_confront_movies(stock / 'StreamingAssets', output, progress, cancelled, fingerprints=confront_recipe)
    )

    # 4. Verify all remaining missing files are covered by the compact core patch, then apply it
    missing = {name for name in expected if not (media / name).is_file()}
    if not missing <= packaged.keys():
        absent = sorted(missing - packaged.keys())
        raise PatchError('Missing files outside the core patch: ' + ', '.join(absent[:8]))
    apply_core_package(package, stock, media, tool_path(), progress, cancelled)

    # 5. Final inventory verification
    actual = {
        p.relative_to(media).as_posix().casefold()
        for p in media.rglob('*')
        if p.is_file()
    }
    actual = {
        name for name in actual
        if not name.startswith(('streamingassets/russian/', 'streamingassets/russianvoice/'))
    }
    expected_lower = {name.casefold() for name in expected}
    if not expected_lower <= actual:
        missing_final = sorted(expected_lower - actual)
        raise PatchError(
            f'Finished Media is missing {len(missing_final)} required file(s): ' + ', '.join(missing_final[:8])
        )

    report.update(
        complete_media=True,
        missing_reference_files=[],
        remaining_stages=[],
        patched_files=len(packaged),
        message='Complete build24 Media created.',
    )
    (output / 'conversion-status.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'\nComplete build24 Media created: {len(expected)} base files in {media}', flush=True)
    print('Copy the Media folder to ux0:app/OBJECTION/Media on your PS Vita.', flush=True)
    return media


def translate(args, media, cancelled):
    output = args.output.resolve()
    if args.mode == 'russian':
        report = prepare_russian(args.russian, media, output, progress, cancelled)
    else:
        destination = media / 'StreamingAssets/Russian'
        with tempfile.TemporaryDirectory(prefix='.translation-', dir=output) as directory:
            temp = Path(directory)
            report = prepare_russian(args.russian, media, temp, progress, cancelled)
            staging = temp / 'Media/StreamingAssets/Russian'
            paths = (staging / 'files.txt').read_text(encoding='utf-8').splitlines()
            if destination.exists():
                shutil.rmtree(destination)
            publish_overlay(staging, destination, paths, cancelled)
    target_media = output / 'Media' if args.mode == 'russian' else media
    if (target_media / 'resources.assets').is_file():
        patch_font(target_media, None)
    patch_title_logo(target_media, True)
    if args.patch.is_file():
        rv_count = apply_russian_voice(args.patch.resolve(strict=True), target_media, progress, cancelled)
        report['russian_voice_files'] = rv_count
    (output / 'russian-status.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(report['message'], flush=True)


def main():
    nearby = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    default_patch = nearby / PACKAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('original', 'russian', 'both', 'custom'), default='original')
    for name in ('steam', 'russian', 'installed', 'output', 'ffmpeg', 'font'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--translation', type=Path, help='PC game folder with a community translation (custom mode)')
    parser.add_argument('--label', help='Language name shown in the Vita settings (custom mode)')
    parser.add_argument('--patch', type=Path, default=default_patch)
    cancelled = Event()
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    interactive = len(sys.argv) == 1 and sys.stdin.isatty()
    try:
        if interactive:
            print('=== Phoenix Wright: Ace Attorney Trilogy - PS Vita Converter (build25) ===')
            print('1. Convert the Steam version')
            print('2. Add Failing Forward Russian translation to an existing Vita Media')
            print('3. Convert Steam and add Failing Forward Russian translation')
            print('4. Install another community translation (auto-detects font & voice-over)')
            options = {'1': 'original', '2': 'russian', '3': 'both', '4': 'custom'}
            mode = options.get(input('Select a mode [1-4]: ').strip())
            if not mode:
                raise PatchError('Enter 1, 2, 3, or 4.')
            args = parser.parse_args(['--mode', mode])
            if mode == 'custom':
                print('\nThe custom language uses the same extra slot as Russian and REPLACES it')
                print('(including the fan Russian voice pack). Re-run option 2 to bring Russian back.')
                args.installed = choose_directory('Select the installed Vita Media folder (or OBJECTION folder)')
                args.translation = choose_directory('Select the translation patch folder (or translated PC game folder)')
                args.label = input('Language name in the settings menu (e.g. Tieng Viet): ').strip()
                print('Optional: a custom .ttf/.otf font (or press Enter to auto-detect from the patch).')
                font = input('Font file: ').strip().strip('"')
                args.font = Path(font) if font else None
            elif mode != 'russian':
                args.steam = choose_directory('Select the Steam installation or PWAAT_Data folder')
            else:
                args.installed = choose_directory('Select the installed Vita Media folder')
            if mode in ('russian', 'both'):
                args.russian = choose_directory('Select the PC installation with Failing Forward')
            if mode != 'custom':
                args.output = choose_directory('Select the output folder (e.g. your OBJECTION folder)')
        else:
            args = parser.parse_args()
        required = {'original': ('steam', 'output'), 'russian': ('installed', 'russian', 'output'),
                    'both': ('steam', 'russian', 'output'),
                    'custom': ('installed', 'translation', 'label')}
        for name in required[args.mode]:
            if getattr(args, name) is None:
                parser.error(f'--{name} is required in {args.mode} mode.')
        if args.mode == 'custom':
            report = install_custom_language(args.steam, args.translation, args.installed, args.label,
                                             args.font, progress, cancelled)
            for item in report['skipped']:
                print(f"  skipped {item['path']}: {item['reason']}", flush=True)
            print(report['message'], flush=True)
            print('Font: ' + report['font'], flush=True)
            if report.get('voice_files'):
                print(f"Voice-over: {report['voice_files']} bank(s) auto-detected and converted to Vita HEVAG.", flush=True)
            else:
                print('Voice-over: none detected in patch (voice option will show a notice).', flush=True)
            return 0
        media = installed_media(args.installed) if args.mode == 'russian' else assemble(args, cancelled)
        if args.mode != 'original':
            translate(args, media, cancelled)
        return 0
    except (Cancelled, KeyboardInterrupt, EOFError):
        print('Conversion canceled.', file=sys.stderr)
        return 130
    except (PatchError, OSError, BadZipFile, ValueError, KeyError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    finally:
        if interactive:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            try:
                input('Press Enter to close.')
            except (KeyboardInterrupt, EOFError):
                pass


if __name__ == '__main__':
    raise SystemExit(main())
