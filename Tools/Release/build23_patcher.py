"""Prepare Media for PWAAT Vita build23 from local inputs."""

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from zipfile import BadZipFile

from patcher.core import PatchError, data_root, inside, sha256
from patcher.group_delta import apply_package
from patcher.tools import tool_path


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def checksums(include_russian=False):
    profile = json.loads((resource_root() / 'recipes/build23-checksums.json').read_text(encoding='utf-8'))
    if profile['reference'] != 'build23':
        raise PatchError('The checksum catalog is for a different build.')
    return {
        name: digest for name, digest in profile['files'].items()
        if include_russian or not name.casefold().startswith(('streamingassets/russian/', 'streamingassets/russianvoice/'))
    }


def check_locations(source, output, expected):
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise PatchError('Choose a separate output folder outside the source installation.')
    media = output / 'Media'
    if media.resolve() != media:
        raise PatchError('The output Media folder must be a regular directory.')
    if media.exists() and not media.is_dir():
        raise PatchError('The output Media path is occupied by a file.')
    unexpected = next((path.relative_to(media).as_posix() for path in media.rglob('*')
                       if path.is_file() and path.relative_to(media).as_posix() not in expected), None)
    if unexpected:
        raise PatchError(f'The output contains an unexpected file: {unexpected}. Choose a new output folder.')
    return media


def verify_tree(media, expected, progress):
    for index, (name, digest) in enumerate(expected.items(), 1):
        path = inside(media, name)
        if not path.is_file() or sha256(path) != digest:
            raise PatchError('Missing or different build23 file: ' + name)
        progress('Verifying build23', index, len(expected), name)


def stage_reference(reference, output, expected, progress):
    reference = reference.resolve(strict=True)
    media = reference / 'Media' if (reference / 'Media').is_dir() else reference
    target = check_locations(reference, output, expected)
    verify_tree(media, expected, progress)
    for index, (name, digest) in enumerate(expected.items(), 1):
        destination = inside(target, name)
        if destination.exists():
            if not destination.is_file() or sha256(destination) != digest:
                raise PatchError('The output contains a different file: ' + name)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                staged = Path(temporary.name)
            try:
                shutil.copyfile(inside(media, name), staged)
                if sha256(staged) != digest:
                    raise PatchError('The source changed during copying: ' + name)
                os.replace(staged, destination)
            finally:
                staged.unlink(missing_ok=True)
        progress('Preparing Media', index, len(expected), name)


def apply_delta(steam, package_path, output, expected, progress):
    steam = steam.resolve(strict=True)
    target = check_locations(steam, output, expected)
    if package_path.resolve().is_relative_to(target):
        raise PatchError('Keep the patch outside the output Media folder.')
    apply_package(package_path, data_root(steam), target, expected, tool_path(), progress)
    verify_tree(target, expected, progress)


def show_progress(phase, done, total, name):
    if total <= 100 or done == 1 or done == total or done % 250 == 0:
        print(f'[{phase}] {done}/{total} {name}', flush=True)


def choose_directory(title):
    import ctypes as ct
    from ctypes import wintypes

    class BrowseInfo(ct.Structure):
        _fields_ = [
            ('owner', wintypes.HWND), ('root', ct.c_void_p),
            ('display_name', wintypes.LPWSTR), ('title', wintypes.LPCWSTR),
            ('flags', wintypes.UINT), ('callback', ct.c_void_p),
            ('callback_data', ct.c_ssize_t), ('image', ct.c_int),
        ]

    shell = ct.windll.shell32
    ole = ct.windll.ole32
    browse = shell.SHBrowseForFolderW
    browse.argtypes, browse.restype = (ct.POINTER(BrowseInfo),), ct.c_void_p
    get_path = shell.SHGetPathFromIDListEx
    get_path.argtypes = (ct.c_void_p, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_path.restype = wintypes.BOOL
    ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = (ct.c_void_p,), None
    if ole.CoInitialize(None) < 0:
        raise PatchError('Windows could not initialize the folder picker.')
    name, path = ct.create_unicode_buffer(260), ct.create_unicode_buffer(32768)
    # Show the resizable Windows picker and allow only filesystem folders.
    info = BrowseInfo(None, None, ct.cast(name, wintypes.LPWSTR), title, 0x51, None, 0, 0)
    item = None
    try:
        item = browse(ct.byref(info))
        if not item:
            raise PatchError('Folder selection canceled.')
        if not get_path(item, path, len(path), 0):
            raise PatchError('Select a folder on a local or mapped drive.')
        return Path(path.value)
    finally:
        if item:
            ole.CoTaskMemFree(item)
        ole.CoUninitialize()


def main(argv=None, *, progress: Callable = show_progress):
    argv = sys.argv[1:] if argv is None else argv
    public = (resource_root() / 'PUBLIC_SOURCE').is_file()
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--reference', type=Path, help='Your existing build23 or build23/Media folder')
    if not public:
        inputs.add_argument('--steam', type=Path, help='Supported clean Steam installation')
        parser.add_argument('--patch', type=Path, help='Private build23 exact delta file')
    parser.add_argument('--include-russian', action='store_true', help='Copy the Russian files from your local reference')
    parser.add_argument('--output', type=Path, required=True, help='Separate output folder')
    directory = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    adjacent = directory / 'build23-exact.pwaatpatch'
    interactive = not argv and sys.stdin.isatty()
    try:
        if interactive:
            use_delta = not public and adjacent.is_file()
            source = choose_directory('Select the Steam installation' if use_delta else 'Select your build23 or Media folder')
            output = choose_directory('Select a separate output folder').resolve()
            expected = checksums()
            if use_delta:
                apply_delta(source, adjacent, output, expected, progress)
            else:
                stage_reference(source, output, expected, progress)
        else:
            args = parser.parse_args(argv)
            output = args.output.resolve()
            expected = checksums(args.include_russian)
            if args.reference:
                if getattr(args, 'patch', None):
                    parser.error('--patch requires --steam')
                stage_reference(args.reference, output, expected, progress)
            else:
                if args.include_russian:
                    parser.error('--include-russian requires a local --reference')
                apply_delta(args.steam, args.patch or adjacent, output, expected, progress)
        print(f'Build23 Media verified: {len(expected)} files in {output / "Media"}', flush=True)
        return 0
    except (OSError, PatchError, ValueError, BadZipFile, KeyError, TypeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if interactive:
            try:
                input('Press Enter to close.')
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == '__main__':
    raise SystemExit(main())
