"""手绘 IM004 比赛通知截图；不使用真实网站或标志。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

im = Image.new('RGB', (900, 1000), '#f1f3f7')
d = ImageDraw.Draw(im)
font = '/System/Library/Fonts/Hiragino Sans GB.ttc'
f19 = ImageFont.truetype(font, 19)
f24 = ImageFont.truetype(font, 24)
f30 = ImageFont.truetype(font, 30)
f42 = ImageFont.truetype(font, 42)
d.rectangle((0, 0, 900, 89), fill='#384c75')
d.text((37, 28), '〈   队伍通知', font=f30, fill='white')
d.text((762, 34), '•••', font=f24, fill='white')
d.rounded_rectangle((36, 121, 864, 924), radius=18, fill='white')
d.text((77, 158), '云隼机器人挑战赛', font=f30, fill='#56647c')
d.text((77, 214), '全国终选入围通知', font=f42, fill='#243852')
d.text((78, 292), '发布：2026年10月15日', font=f24, fill='#8b94a1')
d.line((77, 354, 820, 354), fill='#e3e7ee', width=2)
d.text((77, 396), '队伍：角石队', font=f30, fill='#243852')
d.rounded_rectangle((77, 458, 790, 529), radius=9, fill='#e5f2e9')
d.text((103, 473), '状态：已入围全国终选', font=f30, fill='#36794b')
d.text((77, 582), '终选日期：2026年12月5日', font=f30, fill='#243852')
d.text((77, 640), '开始时间：09:30', font=f30, fill='#243852')
d.text((77, 698), '地点：澄原科技馆主厅', font=f30, fill='#243852')
d.text((77, 779), '入围不代表获奖，奖项待终选后公布。', font=f24, fill='#9e602d')
d.text((77, 859), '请各队按本通知安排参赛', font=f19, fill='#98a0ac')
im.save(Path(__file__).resolve().parent.parent / 'im004_notice.png', optimize=True)
