"""Stage translated binary tables with Vita button prompts and save warnings."""
from pathlib import Path
import argparse,json,struct,zipfile
import bundle_reader as reader

PREFIX='Text/Steam/PWAAT_Data/StreamingAssets/'

def parse(data):
    rows=[];offset=0
    while offset<len(data):
        if offset+2>len(data):raise ValueError('Truncated text ID')
        key=struct.unpack_from('<H',data,offset)[0];offset+=2;start=offset
        while offset+2<=len(data) and data[offset:offset+2]!=b'\0\0':offset+=2
        if offset+2>len(data):raise ValueError('Unterminated text record')
        rows.append([key,data[start:offset].decode('utf-16le')]);offset+=2
    return rows

def encode(rows):return b''.join(struct.pack('<H',key)+text.encode('utf-16le')+b'\0\0' for key,text in rows)

def adapt_text(rel, raw):
    if not rel.startswith('menu/text/'):
        return raw, []
    rows = parse(reader.c.decrypt(raw))
    changes = []
    for row in rows:
        key, text = row
        updated = text
        if rel.endswith('platform_text_u.bin'):
            updated = 'кнопку　' + ('×', '○', '△', 'R')[key % 4]
        elif rel.endswith('save_text_u.bin') and key in (11, 12, 13, 14, 91):
            updated = ('Удаление сохранения...' if key == 91 else 'Идёт сохранение...') + ',Не выключайте PS Vita.'
        elif rel.endswith('option_text_u.bin'):
            for old, new in [('к鸙авишу', 'кнопку'), ('к鸙авиши', 'кнопки'), ('к鸙авиш', 'кнопок'), ('К鸙авиши', 'Кнопки')]:
                updated = updated.replace(old, new)
        elif rel.endswith('system_text_u.bin'):
            for old, new in [('Ｒ１', 'R'), ('Ａ', '×'), ('Ｂ', '○'), ('Ｘ', '△'), ('Ｙ', '□')]:
                updated = updated.replace(old, new)
        if updated != text:
            row[1] = updated.replace('л', '鸙')
            changes.append(key)
    return (reader.c.encrypt(encode(rows)) if changes else raw), changes

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    report={'files':[],'skipped':[],'adapted':[]}
    with zipfile.ZipFile(args.archive) as archive:
        for name in archive.namelist():
            if not name.startswith(PREFIX) or not name.endswith(('.mdt','.bin','.cho')):continue
            rel=name[len(PREFIX):]
            if rel=='menu/text/language_any.bin':
                report['skipped'].append({'path':rel,'reason':'PC patch renames English to Russian; preserve the original nine language labels and append a separate Russian choice in code.'});continue
            raw, changes = adapt_text(rel, archive.read(name))
            if changes:
                report['adapted'].append({'path': rel, 'ids': changes})
            dest=args.output/'Russian'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw);report['files'].append(rel)
    (args.output/'TextReport.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Staged',len(report['files']),'binary files;',len(report['adapted']),'tables adapted for Vita; original nine language names retained.')

if __name__=='__main__':main()
