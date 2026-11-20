"""Build the frame streams used by MovieAccessor in the Unity 2017 PSP2 player."""

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tempfile

import etcpak
import UnityPy

from .core import PatchError, check_cancel, sha256
from .graphics import read_bundle


MOVIES = ('film00', 'film03', 'videonoise')
FPS = 60
WIDTH, HEIGHT = 512, 288
FRAME_SIZE = WIDTH * HEIGHT // 2
HEADER = struct.Struct('<8s6i')
MAGIC = b'PWAVID1\0'
DXT1 = 10


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
                if destination.exists() and (not destination.resolve().is_relative_to(target_dir.resolve())
                                             or sha256(destination, cancel) != digest):
                    raise ValueError('A different frame stream already exists')
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
            if not destination.exists():
                (work / destination.name).rename(destination)
    return results
