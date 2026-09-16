"""Mechanically render fixed synthetic display scenes and retain their exact inputs.

Generation performs no network I/O, model work or account activation. The files
are reproducible test resources, never substitutes for model-produced memories.
"""
from hashlib import sha256
import json
from pathlib import Path
import struct
import zlib

EVENTS = {
    'display-real': (
        ('林青','这是本次合成展台的展板甲。','A'),
        ('林青','这是展板乙。它与甲的颜色布局不同。','B'),
        ('林青','本次展示计划在2030-01-02T10:00:00+08:00开始；希望提前600秒提醒。',None),
        ('周岚','我称林青为阿青，这是这个入口的称呼；不要据此判断其他平台的阿青也是他。',None),
        ('林青','这里只是合成展示记录，没有其他行动结果。',None)),
    'story-fiction': (
        ('叙述者','虚构的雾港故事里，角色青禾看见展板丙。','C'),
        ('叙述者','同一故事中的展板丁没有可见的有色图形。','D'),
        ('叙述者','青禾是虚构角色；不能把这些故事经历记到现实的林青身上。',None),
        ('叙述者','故事中的旧推测‘所有展板都有圆形’与丙、丁的观察不一致。',None),
        ('叙述者','本段仍只是雾港的虚构叙述。',None)),
}
VISIBLE_FACTS = {'A':'左红矩形右蓝圆','B':'左蓝矩形右红圆','C':'上方两个绿矩形、下方黄三角','D':'无有色图形'}
QUERIES = ('合成展板有哪些颜色和形状？','雾港故事里的展板有什么图形？')


def _chunk(kind: bytes, value: bytes) -> bytes:
    return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)


def pixel(scene: str, x: int, y: int) -> tuple[int,int,int]:
    """Evaluate the exact closed geometry at an integer pixel's center."""
    if scene not in VISIBLE_FACTS or not 0<=x<256 or not 0<=y<256:raise ValueError('Invalid synthetic pixel.')
    if scene in ('A','B'):
        if 32<=x<=111 and 80<=y<=175:return (255,0,0) if scene=='A' else (0,0,255)
        if (x-184)**2+(y-128)**2<=32**2:return (0,0,255) if scene=='A' else (255,0,0)
    if scene=='C':
        if (32<=x<=95 or 160<=x<=223) and 32<=y<=95:return (0,255,0)
        # Integer arithmetic represents centers (x+1/2,y+1/2) exactly.
        px,py=2*x+1,2*y+1
        if py>=256 and py<=448 and 3*px+2*py>=1280 and 3*px-2*py<=256:
            return (255,255,0)
    return (255,255,255)


def png_bytes(scene: str) -> bytes:
    """Encode RGB pixels with only IHDR, one IDAT and IEND; no metadata exists."""
    rows=b''.join(b'\0'+bytes(channel for x in range(256) for channel in pixel(scene,x,y)) for y in range(256))
    return b'\x89PNG\r\n\x1a\n'+_chunk(b'IHDR',struct.pack('>IIBBBBB',256,256,8,2,0,0,0))+_chunk(b'IDAT',zlib.compress(rows,9))+_chunk(b'IEND',b'')


def write_materials(root: Path) -> dict:
    """Write exact scene files and a manifest whose slot state remains unapproved."""
    root.mkdir(parents=True,exist_ok=True);images=[]
    for scene,facts in VISIBLE_FACTS.items():
        raw=png_bytes(scene);name=scene+'.png';(root/name).write_bytes(raw)
        pixels=bytes(c for y in range(256) for x in range(256) for c in pixel(scene,x,y))
        images.append({'scene':scene,'path':name,'sha256':sha256(raw).hexdigest(),'bytes':len(raw),
            'pixel_sha256':sha256(pixels).hexdigest(),'visible_facts':facts,'width':256,'height':256,'mode':'RGB'})
    identities={'林青':'synthetic-lin-qing','周岚':'synthetic-zhou-lan','叙述者':'synthetic-narrator'}
    events=[{'entry_id':entry,'ordinal':ordinal,'speaker_label':speaker,'external_subject_id':identities[speaker],
        'body':body,'image_scene':image} for entry,items in EVENTS.items() for ordinal,(speaker,body,image) in enumerate(items,1)]
    value={'material_version':1,'material_origin':'SYNTHETIC_FIXTURE','images':images,'events':events,'queries':QUERIES,
        'goal_comparisons':[['核对合成展板的颜色','检查合成展板颜色是否正确'],['核对展板颜色','清点展板数量']],
        'authorization':'NOT_APPROVED','slots':{'MEDIA':4,'LEARNING':12,'GOAL_DEDUP':2,'EMBEDDING_DOCUMENT':12,'EMBEDDING_QUERY':2},
        'attempts_used':0,'reminder_sink':'DISABLED'}
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    (root/'materials.json').write_bytes(raw)
    return {'path':str(root/'materials.json'),'sha256':sha256(raw).hexdigest(),'bytes':len(raw),'attempts_used':0}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    print(json.dumps(write_materials(parser.parse_args().root),sort_keys=True))
