"""手绘 IM007 改期确认条；一张横向便笺，不套日程表布局。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (1000, 560), '#e8e5df')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f20 = ImageFont.truetype(font, 20)
f26 = ImageFont.truetype(font, 26)
f34 = ImageFont.truetype(font, 34)
f46 = ImageFont.truetype(font, 46)
d.polygon(((44, 26), (958, 26), (971, 527), (30, 527)), fill='#fff7d5')
d.rectangle((390, 10, 623, 45), fill='#bed2cb')
d.text((78, 64), '改期确认  ·  2026年10月14日', font=f26, fill='#7f7050')
d.text((78, 122), '《古航海信札》', font=f34, fill='#544735')
d.text((78, 183), '全稿提交调整为', font=f26, fill='#7f7050')
d.text((78, 230), '11月25日 20:00', font=f46, fill='#a4543e')
d.line((79, 296, 849, 296), fill='#d7be8d', width=2)
d.text((78, 324), '原 11月18日 的全稿期限不再适用。', font=f26, fill='#544735')
d.text((78, 377), '样章、线上终审时间不变。双方已确认。', font=f26, fill='#544735')
d.text((78, 463), '请与首版日程一起保存', font=f20, fill='#9c8d6f')
im.save(Path(__file__).resolve().parent.parent / 'im007_change.png', optimize=True)
