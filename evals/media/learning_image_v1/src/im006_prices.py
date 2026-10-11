"""手绘 IM006 地毯护理价目板；独立黑板布局。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (1050, 830), '#c2ab85')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f20 = ImageFont.truetype(font, 20)
f28 = ImageFont.truetype(font, 28)
f36 = ImageFont.truetype(font, 36)
f46 = ImageFont.truetype(font, 46)
d.rounded_rectangle((25, 24, 1025, 807), radius=15, fill='#263d3b')
d.text((87, 69), '砾光织物护理', font=f46, fill='#faf3da')
d.text((90, 140), '2026年11月1日起执行', font=f28, fill='#c0d8cd')
d.line((84, 204, 960, 204), fill='#789088', width=3)
d.text((90, 248), '机织地毯', font=f36, fill='#faf3da')
d.text((623, 248), '18元 / 平方米', font=f36, fill='#edd391')
d.text((90, 337), '羊毛地毯', font=f36, fill='#faf3da')
d.text((623, 337), '35元 / 平方米', font=f36, fill='#edd391')
d.text((90, 434), '起洗面积：2平方米', font=f28, fill='#c0d8cd')
d.text((90, 493), '3公里内，取送免费', font=f28, fill='#c0d8cd')
d.rounded_rectangle((79, 579, 967, 683), radius=12, outline='#e4b994', width=3)
d.text((111, 602), '不接真丝地毯', font=f36, fill='#ffd6ac')
d.text((89, 749), '请按实际面积计价，取送前确认距离', font=f20, fill='#a9bdb4')
im.save(Path(__file__).resolve().parent.parent / 'im006_prices.png', optimize=True)
