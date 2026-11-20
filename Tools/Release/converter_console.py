"""Windows console entry point for the local PC-to-Vita converter."""

import argparse
import json
from pathlib import Path
import signal
import sys
from threading import Event

from patcher.core import Cancelled, Package, PatchError
from patcher.local import check_original, check_reference_media, complete_from_delta, complete_from_installed, prepare, recipe_root, reference_layout
from patcher.russian import installed_media, prepare_russian


MODES = ('original', 'russian', 'both')
ADJACENT_PATCH = 'build19-completion.pwaatpatch'


def adjacent_patch():
    if getattr(sys, 'frozen', False):
        package = Path(sys.executable).with_name(ADJACENT_PATCH)
        if package.is_file():
            return package
    return None


def pick_folder(title, *, optional=False, output=False):
    import tkinter as tk
    from tkinter import filedialog

    print(title + '...', flush=True)
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        value = filedialog.askdirectory(parent=root, title=title, mustexist=not output)
    except tk.TclError as error:
        raise PatchError('Could not open the folder picker. Use --help for command-line options.') from error
    finally:
        if root is not None:
            root.destroy()
    if value:
        print(f'  {value}', flush=True)
        return Path(value)
    if not optional:
        raise Cancelled('Folder selection canceled.')
    return None


def menu(*, public=False):
    print('Ace Attorney Trilogy for PS Vita')
    print('1. Convert the original Steam version')
    print('2. Add the Failing Forward Russian translation to an existing Vita installation')
    print('3. Convert Steam and add the Russian translation')
    answer = input('Select a mode [1-3]: ').strip()
    if answer not in {'1', '2', '3'}:
        raise PatchError('Enter 1, 2, or 3.')
    mode = MODES[int(answer) - 1]
    steam = pick_folder('Select the Steam game folder or PWAAT_Data') if mode != 'russian' else None
    russian = pick_folder('Select the PC game folder with the Failing Forward translation') if mode != 'original' else None
    patch = adjacent_patch() if mode == 'original' and not public else None
    if russian:
        installed = pick_folder('Select the installed Vita Media folder')
    elif patch:
        print(f'Using the local completion package {patch.name}.')
        installed = None
    elif not public:
        reference = reference_layout()['reference']
        print(f'Select Vita Media from {reference} for a complete conversion, or click Cancel to use Steam files only.')
        installed = pick_folder(f'Select Vita Media from {reference} (optional)', optional=True)
    else:
        installed = None
    output = pick_folder('Select or create a separate output folder', output=True)
    return argparse.Namespace(mode=mode, steam=steam, russian=russian, installed=installed, output=output, patch=patch)


def arguments(argv, *, public=False):
    parser = argparse.ArgumentParser(description='Local Ace Attorney Trilogy converter for PS Vita.',
                                     epilog='Standalone stages: local-player --help and movies --help.')
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--steam', type=Path, help='Clean Steam game folder or its PWAAT_Data folder')
    parser.add_argument('--russian', type=Path, help='PC game folder with the Failing Forward translation')
    parser.add_argument('--installed', type=Path, help='Existing Vita Media for a complete conversion or the translation')
    parser.add_argument('--ffmpeg', type=Path, help='FFmpeg executable for converting Steam videos')
    if not public:
        parser.add_argument('--patch', type=Path, help='Local completion package for original mode')
    parser.add_argument('--output', type=Path, required=True, help='Separate output folder')
    args = parser.parse_args(argv)
    required = (['steam'] if args.mode != 'russian' else []) + (
        ['russian', 'installed'] if args.mode != 'original' else [])
    missing = [f'--{name}' for name in required if getattr(args, name) is None]
    if missing:
        parser.error('Required arguments: ' + ', '.join(missing))
    if args.ffmpeg and (args.mode != 'original' or args.installed):
        parser.error('--ffmpeg requires original mode and no --installed argument')
    if getattr(args, 'patch', None) and (args.mode != 'original' or args.installed):
        parser.error('--patch requires original mode and no --installed argument')
    return args


def progress(phase, done, total, name):
    if done == 1 or done == total or done % 25 == 0:
        print(f'[{phase}] {done}/{total}  {name}', flush=True)


def selected_ffmpeg(candidate=None):
    from patcher.movie_frames import find_ffmpeg
    if not candidate and getattr(sys, 'frozen', False):
        adjacent = Path(sys.executable).with_name('ffmpeg.exe')
        candidate = adjacent if adjacent.is_file() else None
    return find_ffmpeg(candidate)


def movies(argv, cancel):
    from patcher.movie_frames import prepare_stock_movies
    parser = argparse.ArgumentParser(prog='PWAATVitaConverter movies',
                                     description='Convert three Steam videos into PSP2 frame streams.')
    parser.add_argument('--steam', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path)
    args = parser.parse_args(argv)
    ffmpeg = selected_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        raise PatchError('Place ffmpeg.exe beside the converter or specify --ffmpeg.')
    stock, output = check_original(args.steam), args.output.resolve()
    result = prepare_stock_movies(stock / 'StreamingAssets', output, ffmpeg, progress, cancel)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


def run(args, cancel, *, public=False):
    stock = check_original(args.steam) if args.mode != 'russian' else None
    native = installed_media(args.installed) if args.installed else None
    patch = getattr(args, 'patch', None)
    if public and patch:
        raise PatchError('The public converter does not accept binary completion packages.')
    if not public and not native and args.mode == 'original' and not patch:
        patch = adjacent_patch()
    output = args.output.resolve()
    print('Output:', output / 'Media', flush=True)
    original_report = None
    if native and (output == native or native in output.parents or output in native.parents):
        raise PatchError('Choose a separate output folder outside the installed Vita Media.')
    if stock:
        graphics_exclusions = set()
        if native:
            reference = check_reference_media(native)['reference']
            check_reference_media(output / 'Media', existing_output=True)
            print(f'Using your Vita Media from {reference} for files the converter cannot derive from Steam yet.', flush=True)
        elif patch:
            if not patch.is_file():
                raise PatchError(f'Completion package not found: {patch}')
            package = Package(patch)
            try:
                prefix = 'streamingassets/'
                graphics_exclusions = {entry['path'].lower().removeprefix(prefix)
                                       for entry in package.manifest['entries']
                                       if entry.get('replaces_sha256') and entry['path'].lower().startswith(prefix)}
            finally:
                package.close()
            print('Steam files verified. The local delta will supply the remaining Media files.', flush=True)
        else:
            print('Steam files verified. This run will produce a partial Media folder.', flush=True)
        ffmpeg = None
        if not native:
            ffmpeg = selected_ffmpeg(getattr(args, 'ffmpeg', None))
            if patch and not ffmpeg:
                raise PatchError('FFmpeg is required for the complete kit. Keep ffmpeg.exe beside the converter.')
            if not ffmpeg:
                print('To convert the three videos, place ffmpeg.exe beside the converter or add it to PATH.', flush=True)
        original_report = prepare(stock, output, progress, cancel, ffmpeg=ffmpeg, steam_only=not native,
                                  graphics_exclusions=graphics_exclusions)
        if native:
            original_report = complete_from_installed(native, output, original_report, progress, cancel)
        elif patch:
            original_report = complete_from_delta(patch, stock, output, original_report, progress, cancel)
    if args.mode != 'original':
        russian = prepare_russian(args.russian, native, output, progress, cancel)
        (output / 'russian-status.json').write_text(json.dumps(russian, ensure_ascii=False, indent=2), encoding='utf-8')
        print(russian['message'], flush=True)
        print('The selected PC translation does not include Russian voice audio.', flush=True)
    if original_report is not None:
        print(original_report['message'], flush=True)
        return 0 if original_report['complete_media'] else 2
    return 0


def main(argv=None, *, public=False):
    public = public or Path(__file__).with_name('PUBLIC_SOURCE').is_file()
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    argv = list(sys.argv[1:] if argv is None else argv)
    interactive = not argv and sys.stdin.isatty()
    if argv == ['--self-test']:
        from patcher.audio import tools
        from UnityPy.files.ObjectReader import ObjectReader
        from patcher import local_player
        from patcher.movie_frames import steam_video_payload
        import UnityPy
        assert callable(ObjectReader.parse_as_object)
        assert UnityPy
        if not public:
            assert all(path.is_file() for path in tools())
        assert all((recipe_root() / file).is_file()
                   for file in ('unchanged.json', 'steam-source.json', 'failing-forward.json',
                                'portable-script-data.json', 'portable-tight-graphics.json',
                                'portable-atlases.json', 'stock-menu.json'))
        assert reference_layout()['files']
        assert Path(local_player.__file__).with_name('LocalVitaUserTypes.cs').is_file()
        assert callable(steam_video_payload)
        print('PASS', flush=True)
        return 0
    stop = Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        if argv[:1] == ['local-player']:
            from patcher import local_player
            code = local_player.main(argv[1:], stop)
        elif argv[:1] == ['movies']:
            code = movies(argv[1:], stop)
        else:
            args = menu(public=public) if interactive else arguments(argv, public=public)
            code = run(args, stop, public=public)
    except Cancelled as error:
        code = 130
        print(str(error) or 'Operation canceled.', file=sys.stderr)
    except (PatchError, OSError, ValueError) as error:
        code = 1
        print(f'Error: {error}', file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        return 130
    if interactive:
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            input('Press Enter to close.')
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == '__main__':
    raise SystemExit(main())
