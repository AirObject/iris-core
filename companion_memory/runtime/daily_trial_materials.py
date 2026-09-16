"""Deterministic, nonsecret synthetic display inputs for a reviewed trial.

Building resources grants no activation or model request. Model-derived document
slots remain unbound until actual reviewed-source or learning receipts exist.
"""
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import MappingProxyType
from PIL import Image

EVENTS=(
    ('display-real',1,'synthetic-lin-qing','林青','这是本次合成展台的展板甲。','A'),
    ('display-real',2,'synthetic-lin-qing','林青','这是展板乙。它与甲的颜色布局不同。','B'),
    ('display-real',3,'synthetic-lin-qing','林青','本次展示计划在2030-01-02T10:00:00+08:00开始；希望提前600秒提醒。',None),
    ('display-real',4,'synthetic-zhou-lan','周岚','我称林青为阿青，这是这个入口的称呼；不要据此判断其他平台的阿青也是他。',None),
    ('display-real',5,'synthetic-lin-qing','林青','这里只是合成展示记录，没有其他行动结果。',None),
    ('story-fiction',1,'synthetic-narrator','叙述者','虚构的雾港故事里，角色青禾看见展板丙。','C'),
    ('story-fiction',2,'synthetic-narrator','叙述者','同一故事中的展板丁没有可见的有色图形。','D'),
    ('story-fiction',3,'synthetic-narrator','叙述者','青禾是虚构角色；不能把这些故事经历记到现实的林青身上。',None),
    ('story-fiction',4,'synthetic-narrator','叙述者','故事中的旧推测‘所有展板都有圆形’与丙、丁的观察不一致。',None),
    ('story-fiction',5,'synthetic-narrator','叙述者','本段仍只是雾港的虚构叙述。',None))
QUERIES=('合成展板有哪些颜色和形状？','雾港故事里的展板有什么图形？')
GOALS=(('核对合成展板的颜色','检查合成展板颜色是否正确'),('核对展板颜色','清点展板数量'))
FACTS=('左红矩形右蓝圆','左蓝矩形右红圆','上方两个绿矩形、下方黄三角','无有色图形')
SLOTS=MappingProxyType({'MEDIA':4,'LEARNING':12,'GOAL_DEDUP':2,'EMBEDDING_DOCUMENT':12,'EMBEDDING_QUERY':2})

def pixels(scene:str) -> bytes:
    """Rasterize exact inclusive integer coordinates without fonts or metadata."""
    if scene not in ('A','B','C','D'):raise ValueError('Unknown synthetic scene')
    result=bytearray()
    for y in range(256):
        for x in range(256):
            color=(255,255,255)
            if scene in ('A','B'):
                if 32<=x<=111 and 80<=y<=175:color=(255,0,0) if scene=='A' else (0,0,255)
                elif (x-184)**2+(y-128)**2<=32**2:color=(0,0,255) if scene=='A' else (255,0,0)
            elif scene=='C':
                if (32<=x<=95 or 160<=x<=223) and 32<=y<=95:color=(0,255,0)
                elif 256<=2*y+1<=448 and 3*(2*x+1)+2*(2*y+1)>=1280 and 3*(2*x+1)-2*(2*y+1)<=256:color=(255,255,0)
            result.extend(color)
    return bytes(result)

def image_resource(scene:str) -> bytes:
    image=Image.frombytes('RGB',(256,256),pixels(scene));output=BytesIO()
    image.save(output,format='PNG',optimize=False,compress_level=9)
    return output.getvalue()

def write_materials(directory:Path) -> dict:
    """Write a fresh bounded directory; existing resources are never overwritten."""
    directory.mkdir(mode=0o700,parents=False,exist_ok=False)
    images=[]
    for scene,fact in zip(('A','B','C','D'),FACTS,strict=True):
        raw=image_resource(scene);path=directory/(scene+'.png')
        with path.open('xb') as stream:stream.write(raw)
        with Image.open(BytesIO(raw)) as decoded:
            decoded.load()
            if decoded.size!=(256,256) or decoded.mode!='RGB' or decoded.info or getattr(decoded,'n_frames',1)!=1 or decoded.tobytes()!=pixels(scene):raise ValueError('Encoded image differs')
        images.append({'scene':scene,'path':path.name,'bytes':len(raw),'sha256':sha256(raw).hexdigest(),
            'pixel_sha256':sha256(pixels(scene)).hexdigest(),'width':256,'height':256,'mode':'RGB','visible_facts':fact})
    events=[dict(zip(('entry_id','ordinal','external_subject_id','speaker_label','body','image_scene'),value,strict=True)) for value in EVENTS]
    goal_at=datetime.fromisoformat('2030-01-02T10:00:00+08:00')
    material={'material_version':1,'material_origin':'SYNTHETIC_FIXTURE','authorization':'NOT_APPROVED','attempts_used':0,
        'images':images,'events':events,'queries':QUERIES,'goal_comparisons':GOALS,'slots':dict(SLOTS),'reminder_sink':'DISABLED',
        'goal_deadline_at_us':int(goal_at.timestamp())*1000000,'lead_seconds':600,
        'batches':tuple({'entry_id':entry,'batch_ordinal':ordinal,'event_arrivals':arrivals,'maximum_memory_outputs':2}
            for entry in ('display-real','story-fiction') for ordinal,arrivals in ((0,(1,2,3)),(1,(4,5))))}
    body=json.dumps(material,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    with (directory/'materials.json').open('xb') as stream:stream.write(body)
    return {'path':str(directory/'materials.json'),'sha256':sha256(body).hexdigest(),'bytes':len(body),'image_count':4,'event_count':10,'attempt_limit':32,'attempts_used':0}

def main() -> None:
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    print(json.dumps(write_materials(parser.parse_args().directory),ensure_ascii=False,sort_keys=True))

if __name__=='__main__':main()
