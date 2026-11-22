"""Install an arbitrary community PC translation as the extra Vita language slot.

The game has one extra language slot (internally ``Language.RUSSIAN``).  The slot
reads its bundles from ``Media/StreamingAssets/Russian``, takes its label from
``Russian/language.txt`` and uses the ``VitaRussian/TextFont`` font from
``resources.assets``.  This module fills that slot with any PC translation that
overrides the English StreamingAssets files, without needing a per-translation recipe.
"""

import hashlib
import io
import json
import shutil
import struct
import sys
import tempfile
from pathlib import Path

from .core import PatchError, check_cancel
from .local import locate, recipe_root
from .russian import installed_media, publish_overlay


LABEL_FILE = 'language.txt'
FONT_BACKUP = 'resources.assets.default-font'
LOGO_BACKUP = 'globalgamemanagers.default-logo'
TEXT_EXTENSIONS = ('.mdt', '.bin', '.cho')
VOICE_FOLDERS = ('sound/',)  # handled separately by install_custom_voices
# The 13 shout voice banks loaded from StreamingAssets/RussianVoice/Sound/se/wsd/
STOCK_VOICE_HASHES = {
    'sound/se/wsd/se100.unity3d': '5681de0245143a7dc421a34c775bdbf008e5087fea9c30f9dbc199109c5061c4',
    'sound/se/wsd/se101.unity3d': '7fd5bad23ac9fc08f18160734c8c31e97d6eef6887e6d59fde46c350d8de91c1',
    'sound/se/wsd/se102.unity3d': 'f6b2ed295d949bcefd668b337c5e0dc0fe0d9b7cd2ce6c984c661ac7668f4668',
    'sound/se/wsd/se103.unity3d': '8c1513c09c76389502db02149be147fcd4077a83578dd03ea7558882d7669c3f',
    'sound/se/wsd/se104.unity3d': '131b31034b6ae1ba1f9854a3fb4ffc0f84c09db233382199a6f8c845b74520f8',
    'sound/se/wsd/se105.unity3d': '7f2186bfd995ab3ac766c2e62ff48f98953965acc2f450fb0f4017e2f9326f4a',
    'sound/se/wsd/se106.unity3d': '7be773b7a049aab5521e16d48946e5de873f5249b4ba21cab3797c3c51bea350',
    'sound/se/wsd/se107.unity3d': 'fec43a34347a604ebd7c1da197b8cca9245a6ba170cc9bbb88928149932db3e4',
    'sound/se/wsd/se108.unity3d': '752b5feb95fab9fb5d2c8636db177bf5b1c75a77604ed3eb35642edcb3a9241f',
    'sound/se/wsd/se109.unity3d': '0c29a49a5a50b12efd4297dd09d9194ee48023876f159117e85478b9049d5584',
    'sound/se/wsd/se10a.unity3d': 'c2d55d11f3a8349127d7f7bfbaac1bd4cbd62abf036ff337ca66737af8f5e947',
    'sound/se/wsd/se10b.unity3d': '5d1ea9a9855869c83e25f87fd220b9270e4a1395449394bffa72dbf9f1945bfb',
    'sound/se/wsd/se10c.unity3d': '29ab97e403adae708558be7c5e74623d8fd7238b4562ab76b46312fe0d351a6b',
}
# Not usable on Vita: PC/PS4-only button banks and the PC patch's renamed language list.
SKIPPED = {
    'menu/common/court_record_03_pcu.unity3d',
    'menu/common/court_record_02_ps4u.unity3d',
    'menu/text/language_any.bin',
}


def _localization():
    folder = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[3])) / 'Tools/Localization'
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


def locate_translation(folder):
    """Find the PWAAT_Data folder of a translation package (layouts vary)."""
    folder = Path(folder).resolve(strict=True)
    candidates = [folder, folder / 'PWAAT_Data', folder / 'Localization/PWAAT_Data']
    if folder.name.lower() == 'streamingassets':
        candidates.append(folder.parent)
    for candidate in candidates:
        if (candidate / 'StreamingAssets').is_dir():
            return candidate
    raise PatchError('Select the translation folder that contains PWAAT_Data/StreamingAssets.')


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _steam_streaming_hashes():
    """Return {relative_lower: steam_sha256} for stock Steam StreamingAssets from embedded recipes."""
    hashes = {}
    root = recipe_root()
    for name in (
        'unchanged.json',
        'stock-menu.json',
        'portable-graphics.json',
        'portable-tight-graphics.json',
        'portable-atlases.json',
        'portable-streaming-ext.json',
        'portable-script-data.json',
    ):
        path = root / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        entries = data.get('files', data.get('bundles', data.get('entries', [])))
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and item.get('path') and item.get('sha256'):
                    hashes[item['path'].lower()] = item['sha256'].lower()
    return hashes


def _steam_sharedassets_hash():
    path = recipe_root() / 'steam-source.json'
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    for item in data.get('files', []):
        if item.get('path') == 'sharedassets0.assets':
            return item.get('sha256', '').lower()
    return None


def changed_files(original, translated, templates=None, cancel=None):
    """Return (changed, added, ignored, sources) for files under StreamingAssets.

    Some translations ship decrypted bundles as ``name.unity3d.dec``; they are
    treated as ``name.unity3d``. ``sources`` maps each relative path to its real file.
    When ``original`` (Steam StreamingAssets) is None, embedded recipe hashes are used
    to skip unmodified stock Steam files automatically.
    """
    stock_hashes = None if original is not None else _steam_streaming_hashes()
    changed, added, ignored, sources = [], [], [], {}
    for path in sorted(translated.rglob('*')):
        check_cancel(cancel)
        if not path.is_file():
            continue
        relative = path.relative_to(translated).as_posix()
        if relative.lower().endswith('.dec'):
            relative = relative[:-4]
        if not relative.lower().endswith(('.unity3d', *TEXT_EXTENSIONS)):
            continue
        if relative.lower() in SKIPPED or relative.lower().startswith(VOICE_FOLDERS):
            ignored.append(relative)
            continue
        if relative in sources and path.suffix.lower() == '.dec':
            continue
        sources[relative] = path
        if original is not None:
            source = original / relative
            if not source.is_file():
                added.append(relative)
            elif source.stat().st_size != path.stat().st_size or _digest(source) != _digest(path):
                changed.append(relative)
        else:
            stock_digest = stock_hashes.get(relative.lower())
            if stock_digest is not None:
                if _digest(path) != stock_digest:
                    changed.append(relative)
            elif templates is not None and (templates / relative).is_file():
                changed.append(relative)
            else:
                added.append(relative)
    return changed, added, ignored, sources

TTF_MAGICS = (b'\x00\x01\x00\x00', b'OTTO', b'true', b'ttcf')


def _validate_font_bytes(data):
    if len(data) < 12 or data[:4] not in TTF_MAGICS:
        return None
    from PIL import ImageFont
    try:
        font = ImageFont.truetype(io.BytesIO(data), 32)
        name = ' '.join(part for part in font.getname() if part).strip()
        return name or 'CustomFont'
    except Exception:
        return None


def _extract_font_from_unityfs(path):
    """Extract an embedded TTF/OTF font from a UnityFS AssetBundle (even if TypeTree fails)."""
    from UnityPy.helpers import CompressionHelper

    raw = Path(path).read_bytes()
    if not raw.startswith(b'UnityFS\x00'):
        return None, None
    pos = 0

    def read_cstr():
        nonlocal pos
        end = raw.index(b'\x00', pos)
        text = raw[pos:end].decode('ascii', errors='replace')
        pos = end + 1
        return text

    read_cstr()  # UnityFS
    pos += 4     # format version
    read_cstr()  # player version
    read_cstr()  # generator version
    _, c_info, u_info, flags = struct.unpack_from('>QIII', raw, pos)
    pos += 20
    ctype = flags & 0x3F
    if flags & 0x80:
        blocks_info_bytes = raw[len(raw) - c_info:]
        data_start = pos
    else:
        blocks_info_bytes = raw[pos:pos + c_info]
        data_start = pos + c_info
    blocks_info = CompressionHelper.DECOMPRESSION_MAP[ctype](blocks_info_bytes, u_info)
    bp = 16  # 16-byte uncompressed data hash
    bcount = struct.unpack_from('>I', blocks_info, bp)[0]
    bp += 4
    out = bytearray()
    dp = data_start
    for _ in range(bcount):
        u_sz, c_sz, bflags = struct.unpack_from('>IIH', blocks_info, bp)
        bp += 10
        chunk = raw[dp:dp + c_sz]
        dp += c_sz
        out.extend(CompressionHelper.DECOMPRESSION_MAP[bflags & 0x3F](chunk, u_sz))
    for magic in (b'\x00\x01\x00\x00', b'OTTO'):
        idx = 4
        while True:
            hit = out.find(magic, idx)
            if hit < 0:
                break
            length = struct.unpack_from('<I', out, hit - 4)[0]
            if 10_000 <= length <= len(out) - hit:
                candidate = bytes(out[hit:hit + length])
                name = _validate_font_bytes(candidate)
                if name:
                    return candidate, name
            idx = hit + 4
    return None, None


def _extract_modified_sharedassets_font(original_pwaat, patched_pwaat):
    """If the PC translation modified FOT-ModeMinALargeStd-R in sharedassets0.assets, extract it."""
    import UnityPy

    mod_sa = patched_pwaat / 'sharedassets0.assets'
    if not mod_sa.is_file():
        return None, None
    if original_pwaat is not None:
        src_sa = original_pwaat / 'sharedassets0.assets'
        if not src_sa.is_file():
            return None, None
        if src_sa.stat().st_size == mod_sa.stat().st_size and _digest(src_sa) == _digest(mod_sa):
            return None, None
    else:
        stock_hash = _steam_sharedassets_hash()
        if stock_hash and _digest(mod_sa) == stock_hash:
            return None, None

    def read_fonts(path):
        found = {}
        env = UnityPy.load(str(path))
        for obj in env.objects:
            if obj.type.name == 'Font':
                data = obj.read()
                raw = bytes(getattr(data, 'm_FontData', b'') or b'')
                if raw:
                    found[getattr(data, 'm_Name', '')] = raw
        return found

    try:
        mod_fonts = read_fonts(mod_sa)
        orig_fonts = read_fonts(original_pwaat / 'sharedassets0.assets') if original_pwaat is not None else None
    except Exception:
        return None, None
    for name, raw in mod_fonts.items():
        if orig_fonts is not None:
            if orig_fonts.get(name) == raw:
                continue
        else:
            # Stock Steam FOT-ModeMinALargeStd-R is 2,749,440 bytes; only pick modified main font.
            if 'ModeMinA' not in name or len(raw) == 2749440:
                continue
        valid = _validate_font_bytes(raw)
        if valid:
            return raw, f'{valid} (from sharedassets0.assets)'
    return None, None


def detect_patch_font(original_pwaat, translated_root, patched_pwaat):
    """Auto-detect a font shipped inside a community PC translation."""
    # 1. Modified font inside PWAAT_Data/sharedassets0.assets
    data, label = _extract_modified_sharedassets_font(original_pwaat, patched_pwaat)
    if data:
        return data, label

    # 2. Standalone .ttf/.otf or UnityFS font bundle inside the translation folder
    roots = []
    for candidate in (Path(translated_root).resolve(), patched_pwaat.parent, patched_pwaat):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)

    for root in roots:
        for ext in ('*.ttf', '*.otf'):
            for path in sorted(root.rglob(ext)):
                if 'streamingassets' in [p.lower() for p in path.parts]:
                    continue
                raw = path.read_bytes()
                valid = _validate_font_bytes(raw)
                if valid:
                    return raw, f'{valid} (from {path.name})'
        for rel in ('Font/Font.unity3d', 'Font/font', 'Localization/Font/Font.unity3d', 'Localization/Font/font'):
            bundle = root / rel
            if bundle.is_file():
                try:
                    raw, valid = _extract_font_from_unityfs(bundle)
                    if raw:
                        return raw, f'{valid} (from {rel})'
                except Exception:
                    pass
    return None, None


def patch_font(media, font, progress=None, *, font_bytes=None, font_label=None):
    """Replace the extra-language font inside resources.assets (None restores the default)."""
    import UnityPy

    assets = media / 'resources.assets'
    backup = media / FONT_BACKUP
    if font is None and font_bytes is None:
        if backup.is_file():
            shutil.copyfile(backup, assets)
            backup.unlink()
        return 'default'
    if font_bytes is not None:
        data = font_bytes
        name = font_label or 'CustomFont'
    else:
        font = Path(font).resolve(strict=True)
        raw = font.read_bytes()
        if raw.startswith(b'UnityFS\x00'):
            data, extracted_name = _extract_font_from_unityfs(font)
            if not data:
                raise PatchError('No TrueType/OpenType font found inside Unity bundle: ' + font.name)
            name = f'{extracted_name} (from {font.name})'
        else:
            if not _validate_font_bytes(raw):
                raise PatchError('The selected file is not a TrueType/OpenType font: ' + font.name)
            data = raw
            name = font.name
    if not backup.is_file():
        shutil.copyfile(assets, backup)
    env = UnityPy.load(str(backup))  # always patch from the pristine file
    for obj in env.objects:
        if obj.type.name != 'Font':
            continue
        tree = obj.read_typetree()
        if tree['m_Name'] != 'TextFont':
            continue
        tree['m_FontData'] = data
        tree['m_FontNames'] = [name.split(' (from ')[0]]
        obj.save_typetree(tree)
        break
    else:
        raise PatchError('resources.assets has no TextFont object.')
    patched = env.file.save()
    temporary = assets.with_name(assets.name + '.tmp')
    temporary.write_bytes(patched)
    temporary.replace(assets)
    return name


def patch_title_logo(media, enable_russian_logo):
    """Enable or disable the built-in Russian TitleLogo in globalgamemanagers without rebuilding Unity."""
    import UnityPy

    ggm = media / 'globalgamemanagers'
    backup = media / LOGO_BACKUP
    if enable_russian_logo:
        if backup.is_file():
            shutil.copyfile(backup, ggm)
            backup.unlink()
        return
    if not ggm.is_file():
        return
    if not backup.is_file():
        shutil.copyfile(ggm, backup)
    env = UnityPy.load(str(backup))
    changed = False
    for obj in env.objects:
        if obj.type.name != 'ResourceManager':
            continue
        tree = obj.read_typetree()
        container = tree.get('m_Container', [])
        filtered = [entry for entry in container if str(entry[0]).lower() != 'vitarussian/titlelogo']
        if len(filtered) != len(container):
            tree['m_Container'] = filtered
            obj.save_typetree(tree)
            changed = True
        break
    if changed:
        patched = env.file.save()
        temporary = ggm.with_name(ggm.name + '.tmp')
        temporary.write_bytes(patched)
        temporary.replace(ggm)


def install_custom_voices(original_streaming, patched_streaming, templates, progress=None, cancel=None):
    """Auto-detect modified shout voice banks (se100..se10c) and convert them to Vita HEVAG."""
    from . import audio

    candidates = []
    for rel_lower, stock_hash in sorted(STOCK_VOICE_HASHES.items()):
        rel = 'Sound/se/wsd/' + rel_lower.rsplit('/', 1)[-1]
        src = patched_streaming / rel
        if not src.is_file():
            dec = patched_streaming / (rel + '.dec')
            if dec.is_file():
                src = dec
            else:
                continue
        if original_streaming is not None:
            orig = original_streaming / rel
            if orig.is_file() and orig.stat().st_size == src.stat().st_size and _digest(orig) == _digest(src):
                continue
        elif _digest(src) == stock_hash:
            continue
        candidates.append((rel, src))

    voice_dir = templates / 'RussianVoice'
    if voice_dir.exists():
        shutil.rmtree(voice_dir)
    if not candidates:
        return [], []

    try:
        decoder, encoder = audio.tools()
    except Exception as error:
        return [], [{'path': rel, 'reason': f'audio tools unavailable: {error}'} for rel, _ in candidates]

    written = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='.voice-', dir=templates) as temp:
        staging = Path(temp) / 'RussianVoice'
        staging.mkdir()
        for index, (rel, src) in enumerate(candidates, 1):
            check_cancel(cancel)
            try:
                raw, _ = audio.convert_bundle(src, decoder, encoder, cancel)
            except Exception as error:
                errors.append({'path': rel, 'reason': f'{type(error).__name__}: {error}'})
                continue
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            written.append(rel)
            if progress:
                progress('Custom voice', index, len(candidates), rel)
        if written:
            (staging / 'files.txt').write_text('\n'.join(sorted(written, key=str.lower)) + '\n', encoding='utf-8')
            (staging / 'voice-validated.txt').write_text('ok\n', encoding='utf-8')
            check_cancel(cancel)
            publish_overlay(staging, voice_dir, [*written, 'voice-validated.txt'], cancel)
    return written, errors


def install_custom_language(steam, translated, installed, label, font=None, progress=None, cancel=None):
    label = (label or '').strip()
    if not label or len(label) > 24 or '\n' in label or '\r' in label:
        raise PatchError('Enter a language name of 1-24 characters.')
    original = locate(steam) if steam else None
    patched = locate_translation(translated)
    if original is not None and original == patched:
        raise PatchError('The translated PC folder must differ from the original Steam folder.')
    media = installed_media(installed)
    templates = media / 'StreamingAssets'

    orig_streaming = (original / 'StreamingAssets') if original is not None else None
    changed, added, ignored, sources = changed_files(orig_streaming, patched / 'StreamingAssets', templates, cancel)
    if not changed:
        raise PatchError('The translated folder has no files that differ from the stock Steam game.')

    _localization()
    from convert_failing_forward import convert_bundle

    destination = templates / 'Russian'
    skipped = [{'path': path, 'reason': 'not present in the Steam game'} for path in added]
    skipped += [{'path': path, 'reason': 'PC-only or audio file'} for path in ignored]
    written = []
    with tempfile.TemporaryDirectory(prefix='.language-', dir=templates) as temp:
        staging = Path(temp) / 'Russian'
        staging.mkdir()
        for index, relative in enumerate(changed, 1):
            check_cancel(cancel)
            source = sources[relative]
            try:
                raw = source.read_bytes()
                if relative.lower().endswith('.unity3d'):
                    if not (templates / relative).is_file():
                        skipped.append({'path': relative, 'reason': 'no matching bundle in the Vita Media'})
                        continue
                    raw, _ = convert_bundle(relative, raw, orig_streaming, templates, None)
            except Exception as error:
                skipped.append({'path': relative, 'reason': f'{type(error).__name__}: {error}'})
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            written.append(relative)
            if progress:
                progress('Custom language', index, len(changed), relative)
        if not written:
            raise PatchError('No translated file could be converted; see the log above.')
        (staging / 'files.txt').write_text('\n'.join(sorted(written, key=str.lower)) + '\n', encoding='utf-8')
        (staging / LABEL_FILE).write_text(label + '\n', encoding='utf-8')
        check_cancel(cancel)
        if destination.exists():
            shutil.rmtree(destination)
        publish_overlay(staging, destination, [*written, LABEL_FILE], cancel)

    # Auto-detect modified shout voice banks (se100..se10c) and convert to Vita HEVAG.
    # If none are present, RussianVoice is removed so fan Russian voices don't play under another language.
    voice_written, voice_errors = install_custom_voices(orig_streaming, patched / 'StreamingAssets', templates, progress, cancel)
    skipped.extend(voice_errors)
    auto_bytes, auto_label = (None, None) if font is not None else detect_patch_font(original, translated, patched)
    font_name = patch_font(media, font, progress, font_bytes=auto_bytes, font_label=auto_label)
    patch_title_logo(media, False)
    return {
        'custom_language': label,
        'files': len(written),
        'voice_files': len(voice_written),
        'skipped': skipped,
        'font': font_name,
        'message': f'"{label}" installed: {len(written)} translated files, {len(voice_written)} voice banks, {len(skipped)} skipped.',
    }
