"""手绘 IM011 裁判注册卡裁切图；号码含汉字及非数字占位，均无效。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (980, 730), '#e9edef')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f18 = ImageFont.truetype(font, 18)
f24 = ImageFont.truetype(font, 24)
f30 = ImageFont.truetype(font, 30)
f40 = ImageFont.truetype(font, 40)
d.rectangle((33, 25, 947, 695), fill='#fffefa')
d.rectangle((33, 25, 947, 126), fill='#526b86')
d.text((67, 48), '裁判注册卡 · 信息截取', font=f40, fill='white')
d.text((69, 160), '姓名：祝圭', font=f30, fill='#354861')
d.text((69, 222), '资格：羽毛球二级裁判', font=f30, fill='#354861')
d.line((66, 289, 912, 289), fill='#c3cdd8', width=2)
d.text((69, 319), '身份证号（虚构无效片段）', font=f24, fill='#7c8692')
d.text((69, 363), 'ZZZZ-样例-', font=f30, fill='#59697b')
d.rectangle((307, 361, 578, 404), fill='#2c3540')
d.text((594, 363), 'Q', font=f30, fill='#59697b')
d.text((69, 430), '联系电话（虚构无效片段）', font=f24, fill='#7c8692')
d.text((69, 474), '00X-', font=f30, fill='#59697b')
d.rectangle((175, 472, 449, 515), fill='#2c3540')
d.text((465, 474), '-样例7', font=f30, fill='#59697b')
d.text((68, 564), '仅截取部分栏位；遮挡处没有可还原的内容。', font=f24, fill='#8c735b')
d.text((68, 642), '号码以无效格式代替；此图不提供可用联系方式。', font=f18, fill='#9a9d9f')
im.save(Path(__file__).resolve().parent.parent / 'im011_card.png', optimize=True)
