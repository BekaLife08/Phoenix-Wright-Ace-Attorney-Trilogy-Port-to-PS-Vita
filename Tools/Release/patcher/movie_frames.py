"""Build the frame streams used by MovieAccessor in the Unity 2017 PSP2 player."""

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tempfile

import etcpak
import UnityPy
from PIL import Image

from .core import PatchError, check_cancel, sha256
from .graphics import read_bundle


MOVIES = ('film00', 'film03', 'videonoise')
FPS = 60
WIDTH, HEIGHT = 512, 288
FRAME_SIZE = WIDTH * HEIGHT // 2
HEADER = struct.Struct('<8s6i')
MAGIC = b'PWAVID1\0'
DXT1 = 10
# Use NPOT heights (260, 508) so PS Vita GXM / Vita3K stores the dynamic
# Texture2D linearly instead of Morton-swizzled, preventing frame-update artifacts.
CONFRONT_MOVIES = (
    ('film01_confront', 512, 260, 12, tuple(f'{i:03d}' for i in range(1, 900, 2))),
    ('film02_confront', 512, 508, 10, tuple(f'{i:05d}' for i in range(1, 1421))),
)


def prepare_confront_movies(streaming, output, progress=None, cancel=None, *, fingerprints=None):
    """Create the two 30 FPS case-five streams from Steam texture bundles."""
    source_dir = streaming / 'GS1/moviescriptable'
    target_dir = output / 'Media/StreamingAssets/GS1/Movie'
    if not source_dir.resolve().is_relative_to(streaming.resolve()) or not target_dir.resolve().is_relative_to(output):
        raise PatchError('Case-five video path points outside the selected folder.')
    target_dir.mkdir(parents=True, exist_ok=True)
    reports = {}
    for name, width, height, fmt, expected in CONFRONT_MOVIES:
        check_cancel(cancel)
        header = HEADER.pack(MAGIC, width, height, 30, 1, len(expected), fmt)
        size = width * height // (2 if fmt == DXT1 else 1)
        bundle = source_dir / (name + '.unity3d')
        destination = target_dir / (name + '.frames.bin')
        if not bundle.resolve().is_relative_to(source_dir.resolve()) or not destination.resolve().is_relative_to(target_dir.resolve()):
            raise PatchError('Case-five video file points outside the selected folder.')
        source_hash = sha256(bundle, cancel)
        key = 'GS1/Movie/' + destination.name
        fingerprint = fingerprints[key] if fingerprints is not None else None
        if fingerprint and source_hash != fingerprint['steam_bundle_sha256']:
            raise PatchError('Unsupported Steam frame bundle: ' + bundle.name)
        if destination.exists() and fingerprint and sha256(destination, cancel) == fingerprint['sha256']:
            reports[key] = dict(fingerprint)
            continue
        with tempfile.TemporaryDirectory(prefix='.vita-confront-', dir=target_dir) as folder:
            staged = Path(folder) / destination.name
            env = read_bundle(bundle.read_bytes())
            frames = {}
            for obj in env.objects:
                if obj.type.name == 'Texture2D':
                    # Object names are short. Reading through the object reader here
                    # avoids decoding full textures until their playback turn.
                    frame_name = obj.peek_name()
                    if frame_name in frames:
                        raise PatchError('Duplicate case-five frame: ' + str(frame_name))
                    frames[frame_name] = obj
            if set(frames) != set(expected):
                raise PatchError('Unexpected frame names in ' + bundle.name)
            with staged.open('xb') as stream:
                stream.write(header)
                for index, frame_name in enumerate(expected, 1):
                    check_cancel(cancel)
                    texture = frames[frame_name].read()
                    if (texture.m_Width, texture.m_Height, texture.m_TextureFormat) == (width, height, fmt):
                        data = texture.get_image_data()[:size]
                    else:
                        image = texture.image.convert('RGBA').resize((width, height), Image.Resampling.LANCZOS)
                        rgba = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
                        data = (etcpak.compress_bc1 if fmt == DXT1 else etcpak.compress_bc3)(rgba, width, height)
                    if len(data) != size:
                        raise PatchError('Unexpected case-five frame size: ' + frame_name)
                    stream.write(data)
                    if progress:
                        progress(name, index, len(expected), frame_name)
            digest = sha256(staged, cancel)
            if fingerprint and digest != fingerprint['sha256']:
                raise PatchError('Case-five frame stream failed verification: ' + key)
            staged.replace(destination)
        reports[key] = {'frames': len(expected), 'fps': 30, 'sha256': digest, 'steam_bundle_sha256': source_hash}
    return reports


def video_payload(bundle):
    env = UnityPy.load(str(bundle))
    clip, = (obj.read() for obj in env.objects if obj.type.name == 'TextAsset')
    data = clip.m_Script.encode('utf-8', 'surrogateescape')
    if not data.startswith(b'\x1aE\xdf\xa3'):
        raise ValueError(f'{bundle.name} does not contain the original WebM')
    return data


def steam_video_payload(bundle):
    env = read_bundle(bundle.read_bytes())
    if len(env.assets) != 1 or int(env.assets[0].target_platform) != 19:
        raise ValueError('Unexpected Steam video bundle platform')
    clips = [obj for obj in env.objects if obj.type.name == 'VideoClip']
    if len(clips) != 1:
        raise ValueError('Expected a single Steam VideoClip')
    resource = clips[0].read_typetree()['m_ExternalResources']
    source = resource['m_Source']
    name = PurePosixPath(source).name
    reader = env.file.files.get(name)
    start, length = resource['m_Offset'], resource['m_Size']
    if not source.startswith('archive:/') or reader is None or not hasattr(reader, 'bytes'):
        raise ValueError('Video resource is not embedded in the bundle')
    if start < 0 or length < 4 or start + length > reader.Length:
        raise ValueError('Video resource range is outside the bundle')
    data = reader.bytes[start:start + length]
    if not data.startswith(b'\x1aE\xdf\xa3'):
        raise ValueError('Video resource is not WebM')
    return data


@contextmanager
def decode(ffmpeg, source, filters):
    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-i', str(source), '-map', '0:v:0', '-vf', filters, '-an',
               '-c:v', 'rawvideo', '-threads:v', '1', '-pix_fmt', 'rgba',
               '-f', 'rawvideo', 'pipe:1']
    with subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as decoder:
        try:
            yield decoder
        finally:
            if decoder.poll() is None:
                decoder.kill()


def compress_frame(rgba):
    if len(rgba) != WIDTH * HEIGHT * 4:
        raise ValueError('Incomplete RGBA frame')
    frame = etcpak.compress_bc1(rgba, WIDTH, HEIGHT)
    if len(frame) != FRAME_SIZE:
        raise ValueError('Unexpected BC1 payload size')
    return frame


def encode(ffmpeg, source, target, interpolate=False, cancel=None):
    # Unity raw textures have their first row at the bottom. The bars preserve
    # the narrower noise clip's proportions in the same 16:9 display.
    scale = (
        f'scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,'
        f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1'
    )
    if interpolate:
        # film03 is a nine-second, 270-frame source. The extra tail supplies
        # motion-estimation lookahead, and trim keeps the original duration.
        filters = (
            f'fps=30,{scale},format=yuv420p,tpad=stop=3:stop_mode=clone,'
            f'minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:vsbmc=1:scd=fdiff,'
            f'trim=end_frame={9 * FPS},vflip'
        )
    else:
        filters = f'fps={FPS},{scale},vflip'
    with decode(ffmpeg, source, filters) as decoder, target.open('wb') as output:
        output.write(bytes(HEADER.size))
        count = 0
        while True:
            check_cancel(cancel)
            rgba = decoder.stdout.read(WIDTH * HEIGHT * 4)
            if not rgba:
                break
            output.write(compress_frame(rgba))
            count += 1
        if decoder.wait() or not count or (interpolate and count != 9 * FPS):
            raise ValueError(f'FFmpeg did not decode {source.name} successfully')
        output.seek(0)
        output.write(HEADER.pack(MAGIC, WIDTH, HEIGHT, FPS, 1, count, DXT1))
    if interpolate:
        # Use exact source images for even frames so 30 FPS playback samples
        # only the original motion and colors.
        with decode(ffmpeg, source, f'fps=30,{scale},vflip') as original, target.open('r+b') as output:
            for index in range(0, count, 2):
                check_cancel(cancel)
                output.seek(HEADER.size + index * FRAME_SIZE)
                output.write(compress_frame(original.stdout.read(WIDTH * HEIGHT * 4)))
            if original.stdout.read(1) or original.wait():
                raise ValueError('Unexpected number of source frames')
    return count


def find_ffmpeg(candidate=None):
    executable = shutil.which(str(candidate) if candidate else 'ffmpeg')
    if not executable and candidate:
        raise PatchError('FFmpeg not found: ' + str(candidate))
    return executable


def prepare_stock_movies(streaming, output, ffmpeg, progress=None, cancel=None):
    game = streaming.parent
    if output == game or game in output.parents or output in game.parents:
        raise PatchError('Choose a separate output folder outside the source game.')
    movie_dir = Path('GS1/Movie')
    source_dir = streaming / movie_dir
    target_dir = output / 'Media/StreamingAssets' / movie_dir
    if not source_dir.resolve().is_relative_to(streaming) or not target_dir.resolve().is_relative_to(output):
        raise PatchError('Movie path points outside the selected folder.')
    target_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix='.vita-movie-', dir=target_dir) as folder:
        work = Path(folder)
        for index, name in enumerate(MOVIES, 1):
            check_cancel(cancel)
            bundle = source_dir / (name + '.unity3d')
            source = work / (name + '.webm')
            target = work / (name + '.frames.bin')
            try:
                if not bundle.resolve().is_relative_to(source_dir.resolve()):
                    raise ValueError('The movie bundle points outside the Steam folder')
                source.write_bytes(steam_video_payload(bundle))
                frames = encode(ffmpeg, source, target, interpolate=(name == 'film03'), cancel=cancel)
                if target.stat().st_size != HEADER.size + frames * FRAME_SIZE:
                    raise ValueError('Frame stream has an unexpected length')
                relative = (movie_dir / target.name).as_posix()
                digest = sha256(target, cancel)
                destination = target_dir / target.name
                if destination.exists() and not destination.resolve().is_relative_to(target_dir.resolve()):
                    raise ValueError('The movie destination points outside the output folder')
                results[relative] = {'frames': frames, 'fps': FPS, 'sha256': digest,
                                     'steam_bundle_sha256': sha256(bundle, cancel)}
            except (OSError, ValueError, KeyError) as error:
                raise PatchError(f'Movie {name}: {error}') from error
            finally:
                source.unlink(missing_ok=True)
            if progress:
                progress('PSP2 movies', index, len(MOVIES), name)
        check_cancel(cancel)
        for relative in results:
            destination = target_dir / Path(relative).name
            (work / destination.name).replace(destination)
    return results
