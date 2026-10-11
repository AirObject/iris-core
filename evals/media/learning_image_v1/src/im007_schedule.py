"""手绘 IM007 首版译作日程；与改期条为两张独立设计。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (1080, 780), '#f4f0f8')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f20 = ImageFont.truetype(font, 20)
f26 = ImageFont.truetype(font, 26)
f34 = ImageFont.truetype(font, 34)
f42 = ImageFont.truetype(font, 42)
d.text((58, 42), '《古航海信札》译作日程', font=f42, fill='#4d396d')
d.text((60, 111), '译者：孔芃  /  2026年10月10日确认版', font=f26, fill='#7c6b91')
d.line((107, 239, 107, 608), fill='#b6a3ce', width=5)
d.ellipse((87, 243, 127, 283), fill='#77579a')
d.ellipse((87, 399, 127, 439), fill='#77579a')
d.ellipse((87, 554, 127, 594), fill='#77579a')
d.rounded_rectangle((159, 206, 1004, 330), radius=12, fill='white')
d.text((184, 224), '10月28日  20:00', font=f34, fill='#4d396d')
d.text((186, 275), '样章提交', font=f26, fill='#746180')
d.rounded_rectangle((159, 361, 1004, 485), radius=12, fill='white')
d.text((184, 379), '11月18日  20:00', font=f34, fill='#4d396d')
d.text((186, 430), '全稿提交', font=f26, fill='#746180')
d.rounded_rectangle((159, 517, 1004, 641), radius=12, fill='white')
d.text((184, 535), '12月2日  14:00', font=f34, fill='#4d396d')
d.text((186, 586), '线上终审', font=f26, fill='#746180')
d.text((62, 714), '年份均为2026年 · 请保留改期确认记录', font=f20, fill='#8b7e99')
im.save(Path(__file__).resolve().parent.parent / 'im007_schedule.png', optimize=True)
