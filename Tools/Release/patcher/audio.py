"""Local PC AudioClip -> PSP2 HEVAG conversion; no native game templates/SDK."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core import PatchError, check_cancel, sha256
from .bundle_crypto import game_cipher_parameters
from .fsb import parse, hevag

RECIPE = 'pwaat-local-audio/1'
AUDIO_SCHEMA = 'c6104784566091ce5562d86ca06c5b89b1f1c6a20a8175a2c23661e42438d595'
# This version has the same encoder and differs only in writing the receipt.
PREVIOUS_RECEIPT_SOURCE = '96fb28bb87ec412a962b01d21bc2b8d31e21d79210825629adf6a973a0bb55c9'


def tools():
    local = os.environ.get('PWAAT_AUDIO_TOOLS')
    if local:
        root = Path(local).expanduser().resolve()
        decoder, encoder = root / 'vgmstream/vgmstream-cli.exe', root / 'HevagEncoder.exe'
        if not decoder.is_file() or not encoder.is_file():
            raise PatchError('PWAAT_AUDIO_TOOLS needs vgmstream/vgmstream-cli.exe and HevagEncoder.exe.')
        return decoder, encoder
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'vendor'
    if not (root / 'audio-tools.json').is_file():
        raise PatchError('Set PWAAT_AUDIO_TOOLS to the folder with your local audio tools.')
    record = json.loads((root / 'audio-tools.json').read_text(encoding='utf-8'))
    for name, digest in record['files'].items():
        if sha256(root / name) != digest:
            raise PatchError('Audio component is corrupted: ' + name)
    return root / 'vgmstream/vgmstream-cli.exe', root / 'HevagEncoder.exe'


def _run(command, cancel=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen([str(x) for x in command], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=flags)
    start = time.monotonic()
    try:
        while True:
            check_cancel(cancel)
            if time.monotonic() - start > 900:
                raise PatchError('Audio conversion timed out')
            try:
                output, _ = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise PatchError('Audio converter error: ' + output.decode('utf-8', 'replace')[-1600:])
        return output.decode('utf-8', 'replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def convert_fsb(data, decoder, encoder, cancel=None):
    sample = parse(data)
    if sample.codec == 9:
        return data, asdict(sample)
    if sample.codec != 15:
        raise PatchError('Unsupported input FSB5 codec: ' + str(sample.codec))
    with tempfile.TemporaryDirectory(prefix='pwaat-audio-') as folder:
        work = Path(folder)
        source, wav, raw = work / 'source.fsb', work / 'decoded.wav', work / 'encoded.raw'
        source.write_bytes(data)
        _run([decoder, '-i', '-o', wav, source], cancel)
        with wave.open(str(wav), 'rb') as pcm:
            layout = (pcm.getnframes(), pcm.getnchannels(), pcm.getframerate(), pcm.getsampwidth())
        if layout != (sample.frames, sample.channels, sample.rate, 2):
            raise PatchError('Decoded PCM does not match the FSB5 header')
        _run([encoder, wav, raw], cancel)
        return hevag(raw.read_bytes(), sample), asdict(sample)


def _plain(data):
    if data.startswith(b'UnityFS'):
        return data
    from Cryptodome.Cipher import AES
    if not data or len(data) % 16:
        raise PatchError('Invalid encrypted bundle size')
    key, iv = game_cipher_parameters()
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad or not plain.startswith(b'UnityFS'):
        raise PatchError('Could not decrypt the game bundle')
    return plain[:-pad]


def convert_bundle(source: Path, decoder, encoder, cancel=None):
    import UnityPy
    from UnityPy.enums import BuildTarget
    from UnityPy.helpers.ResourceReader import get_resource_data
    from UnityPy.streams import EndianBinaryReader
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    env = UnityPy.load(_plain(data))
    if len(env.assets) != 1:
        raise PatchError('Expected a single SerializedFile: ' + str(source))
    asset = env.assets[0]
    if asset.unity_version not in ('2017.4.8f1', '2017.4.2f2'):
        raise PatchError('Unsupported Unity version: ' + asset.unity_version)
    if int(asset.target_platform) not in (5, 19, 30):
        raise PatchError('Unsupported audio bundle platform')
    objects = list(env.objects)
    if not objects or any(o.type.name not in ('AudioClip', 'AssetBundle') for o in objects):
        raise PatchError('Bundle contains assets other than audio: ' + str(source))
    clips = [o for o in objects if o.type.name == 'AudioClip']
    if not clips:
        raise PatchError('Bundle has no AudioClip')
    bundle = next(iter(env.files.values()))
    new_name = 'CAB-' + hashlib.sha256((RECIPE + digest).encode()).hexdigest()[:32]
    resource_name = new_name + '.resource'
    resource = bytearray()
    report = []
    for obj in clips:
        check_cancel(cancel)
        if hashlib.sha256(str(obj.serialized_type.node).encode()).hexdigest() != AUDIO_SCHEMA:
            raise PatchError('Unsupported AudioClip layout')
        tree = obj.read_typetree()
        if tree['m_BitsPerSample'] != 16 or tree['m_SubsoundIndex'] != 0:
            raise PatchError('Unsupported AudioClip parameters')
        stream = tree['m_Resource']
        fsb = get_resource_data(stream['m_Source'], asset, stream['m_Offset'], stream['m_Size'])
        encoded, info = convert_fsb(fsb, decoder, encoder, cancel)
        if (info['channels'], info['rate']) != (tree['m_Channels'], tree['m_Frequency']):
            raise PatchError('AudioClip parameters do not match FSB5')
        offset = len(resource)
        resource.extend(encoded)
        resource.extend(bytes((-len(resource)) % 32))
        tree['m_Resource'] = {'m_Source': 'archive:/' + new_name + '/' + resource_name,
                              'm_Offset': offset, 'm_Size': len(encoded)}
        tree['m_CompressionFormat'] = 5  # Unity AudioCompressionFormat.HEVAG
        tree['m_Length'] = info['frames'] / info['rate']
        obj.save_typetree(tree)
        report.append(dict(name=tree['m_Name'], **info))
    asset.set_version('2017.4.2f2')
    asset.target_platform = BuildTarget.PSP2
    asset._m_target_platform = int(BuildTarget.PSP2)
    asset.name = new_name
    asset.mark_changed()
    bundle.version_engine = '2017.4.2f2'
    # Drop old source resource payloads; output contains only freshly encoded audio.
    resource_file = EndianBinaryReader(bytes(resource))
    resource_file.flags = 0
    bundle.files = {new_name: asset, resource_name: resource_file}
    result = bundle.save(packer='lz4hc')
    check = UnityPy.load(result)
    if len(check.assets) != 1 or int(check.assets[0].target_platform) != 30:
        raise PatchError('Output bundle failed the platform check')
    for obj in check.objects:
        if obj.type.name != 'AudioClip':
            continue
        tree = obj.read_typetree()
        stream = tree['m_Resource']
        info = parse(get_resource_data(stream['m_Source'], obj.assets_file, stream['m_Offset'], stream['m_Size']))
        if info.codec != 9 or tree['m_CompressionFormat'] != 5:
            raise PatchError('Output AudioClip is not HEVAG')
    return result, {'source_sha256': digest, 'clips': report}


def _convert_tree(game: Path, output: Path, progress=None, cancel=None, limit=None, workers=2):
    game, output = game.resolve(), output.resolve()
    candidates = (game / 'StreamingAssets', game / 'PWAAT_Data/StreamingAssets', game)
    streaming = next((p for p in candidates if (p / 'Sound').is_dir()), None)
    if streaming is None:
        raise PatchError('StreamingAssets/Sound folder not found')
    if output == game or game in output.parents or output in game.parents:
        raise PatchError('Choose a separate output folder outside the source game')
    decoder, encoder = tools()
    vendor_record = encoder.parent / 'audio-tools.json'
    audio_components = (sorted(decoder.parent.glob('*.dll')) if os.environ.get('PWAAT_AUDIO_TOOLS')
                        else [vendor_record])
    components = (sha256(Path(__file__).with_name('fsb.py')) + sha256(decoder) + sha256(encoder)
                  + ''.join(sha256(path) for path in audio_components))
    identity = hashlib.sha256((RECIPE + sha256(Path(__file__)) + components).encode()).hexdigest()
    previous_identity = hashlib.sha256((RECIPE + PREVIOUS_RECEIPT_SOURCE + components).encode()).hexdigest()
    marker = output / '.pwaat-local-audio.json'
    state = {'format': RECIPE, 'recipe_sha256': identity, 'complete': {}, 'pending': {}}
    if marker.is_file():
        loaded = json.loads(marker.read_text(encoding='utf-8'))
        if loaded.get('format') == RECIPE and loaded.get('recipe_sha256') in (identity, previous_identity):
            state = loaded
            state['recipe_sha256'] = identity
    files = sorted((streaming / 'Sound').rglob('*.unity3d'))
    if limit is not None:
        if limit < 1:
            raise ValueError('limit must be positive')
        files = files[:limit]
    output.mkdir(parents=True, exist_ok=True)

    def save():
        descriptor, name = tempfile.mkstemp(prefix='.audio-state-', dir=output)
        temp = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(8):
                try:
                    os.replace(temp, marker)
                    break
                except PermissionError as error:
                    if getattr(error, 'winerror', None) not in (5, 32) or attempt == 7:
                        raise
                    check_cancel(cancel)
                    time.sleep(0.1 * (attempt + 1))
        finally:
            temp.unlink(missing_ok=True)

    save()
    converted = skipped = 0
    jobs = []
    for index, source in enumerate(files):
        check_cancel(cancel)
        relative = source.relative_to(streaming).as_posix()
        target = output / 'Media/StreamingAssets' / relative
        if not source.resolve().is_relative_to(streaming.resolve()) or not target.resolve().is_relative_to(output):
            raise PatchError('Link points outside the selected folder: ' + relative)
        digest = sha256(source)
        record = state['pending'].get(relative) or state['complete'].get(relative)
        if record and record['source_sha256'] == digest and target.is_file() and sha256(target) == record['sha256']:
            if relative in state['pending']:
                state['complete'][relative] = state['pending'].pop(relative)
                save()
            skipped += 1
        else:
            jobs.append((source, relative, target, digest))
        if progress:
            progress('Source', index + 1, len(files), relative)
    abort = threading.Event()

    class BatchCancel:
        def is_set(self):
            return abort.is_set() or (cancel is not None and cancel.is_set())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(convert_bundle, source, decoder, encoder, BatchCancel()): (relative, target, digest)
                   for source, relative, target, digest in jobs}
        try:
            for future in as_completed(futures):
                check_cancel(cancel)
                relative, target, digest = futures.pop(future)
                result, record = future.result()
                if record['source_sha256'] != digest:
                    raise PatchError('Source file changed while it was being read: ' + relative)
                if not target.resolve().is_relative_to(output):
                    raise PatchError('Output path changed: ' + relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                record['sha256'] = hashlib.sha256(result).hexdigest()
                state['pending'][relative] = record
                save()  # Ownership precedes file commit for power-loss recovery.
                descriptor, name = tempfile.mkstemp(prefix=target.name + '.', suffix='.converting', dir=target.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(result)
                        stream.flush()
                        os.fsync(stream.fileno())
                    check_cancel(cancel)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                state['complete'][relative] = record
                state['pending'].pop(relative, None)
                save()
                converted += 1
                if progress:
                    progress('Audio', converted + skipped, len(files), relative)
                if converted % 8 == 0:
                    gc.collect()
        except BaseException:
            abort.set()
            for future in futures:
                future.cancel()
            raise
    return {'recipe': RECIPE, 'converted': converted, 'resumed': skipped, 'files': len(files),
            'scope': 'Audio only; full Media converter and hardware validation pending'}


def convert_tree(game: Path, output: Path, progress=None, cancel=None, limit=None, workers=2):
    if not 1 <= workers <= 4:
        raise ValueError('workers must be between 1 and 4')
    game, output = game.resolve(), output.resolve()
    if output == game or game in output.parents or output in game.parents:
        raise PatchError('Choose a separate output folder outside the source game')
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.pwaat-local-audio.lock'
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PatchError('Another conversion is using this folder. If it crashed, close all converter instances before deleting .pwaat-local-audio.lock.') from None
    try:
        os.write(descriptor, str(os.getpid()).encode('ascii'))
        return _convert_tree(game, output, progress, cancel, limit, workers)
    finally:
        os.close(descriptor)
        lock.unlink()
