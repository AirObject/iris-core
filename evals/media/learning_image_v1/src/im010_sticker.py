"""手绘 IM010 缓冲中的云朵表情；无人物照片或个人信息。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (720, 720), '#f5f2fb')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f20 = ImageFont.truetype(font, 20)
f40 = ImageFont.truetype(font, 40)
d.ellipse((100, 263, 310, 469), fill='#ddd4ee')
d.ellipse((218, 183, 452, 433), fill='#ddd4ee')
d.ellipse((377, 255, 614, 471), fill='#ddd4ee')
d.rounded_rectangle((144, 335, 580, 490), radius=62, fill='#ddd4ee')
d.line((232, 337, 276, 337), fill='#655779', width=8)
d.line((414, 337, 458, 337), fill='#655779', width=8)
d.ellipse((327, 383, 371, 427), fill='#9985b1')
d.arc((299, 78, 379, 158), 20, 289, fill='#9d87b7', width=8)
d.polygon(((360, 77), (376, 99), (349, 98)), fill='#9d87b7')
d.ellipse((151, 156, 165, 170), fill='#bba8d0')
d.ellipse((529, 167, 543, 181), fill='#bba8d0')
d.text((173, 539), '脑子正在缓冲…', font=f40, fill='#655779')
d.text((241, 651), '加载中，请稍候', font=f20, fill='#ad9dbc')
im.save(Path(__file__).resolve().parent.parent / 'im010_sticker.png', optimize=True)
