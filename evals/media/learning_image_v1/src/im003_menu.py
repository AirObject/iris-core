"""手绘 IM003 菜单及圈选；每个菜名与线条独立绘制。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (780, 880), '#fff6e5')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f20 = ImageFont.truetype(font, 20)
f26 = ImageFont.truetype(font, 26)
f34 = ImageFont.truetype(font, 34)
f52 = ImageFont.truetype(font, 52)
d.rectangle((25, 26, 755, 854), outline='#527357', width=3)
d.text((242, 65), '小苔饭所', font=f52, fill='#345638')
d.text((267, 140), '午间饭单', font=f26, fill='#79916f')
d.line((75, 202, 705, 202), fill='#8ba077', width=2)
d.text((92, 246), '香煎鸡腿饭', font=f34, fill='#41483b')
d.text((602, 250), '¥36', font=f34, fill='#41483b')
d.text((92, 356), '南瓜烤菇饭', font=f34, fill='#41483b')
d.text((602, 360), '¥32', font=f34, fill='#41483b')
d.ellipse((58, 331, 707, 416), outline='#d06142', width=6)
d.line((652, 417, 668, 447), fill='#d06142', width=5)
d.text((92, 468), '番茄牛腩饭', font=f34, fill='#41483b')
d.text((602, 472), '¥39', font=f34, fill='#41483b')
d.text((93, 576), '海苔豆腐饭', font=f34, fill='#41483b')
d.text((602, 580), '¥29', font=f34, fill='#41483b')
d.line((75, 659, 705, 659), fill='#8ba077', width=2)
d.text((92, 692), '饭类可备注：少饭 / 不放香菜', font=f26, fill='#527357')
d.text((93, 789), '点餐时请告诉店员需要的备注', font=f20, fill='#8d8c7e')
im.save(Path(__file__).resolve().parent.parent / 'im003_menu.png', optimize=True)
