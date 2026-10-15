"""Draw a 16:9 television guide in mpv, leaving the live-video pane uncovered."""
from datetime import datetime
import textwrap

from .model import HALF_HOUR, WINDOW, ROWS, FILTERS, clock, clean

WIDTH, HEIGHT = 1280, 720
PREVIEW = (896, 42, 352, 198)
GRID_X, GRID_Y, GRID_W, ROW_H = 412, 322, 836, 50
# Match the browser guide's charcoal surfaces and lime-green accents.
BG, PANEL, GRID, LINE = '101318', '15191F', '1B2027', '2A3039'
WHITE, MUTED, INK, ACCENT = 'EEF1F5', '929CA9', '192211', 'C7F37A'
ACTIVE, SELECTED, PREVIEW_LINE = '232B24', '20291E', '43523B'


def literal(value):
    # ASS tags are executable formatting. Source titles remain literal text.
    return clean(value).replace('\\', '＼').replace('{', '｛').replace('}', '｝')


def bgr(rgb):
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


class Canvas:
    def __init__(self):
        self.lines = []

    def rect(self, x, y, w, h, color):
        if w > 0 and h > 0:
            self.lines.append(r'{\an7\pos(0,0)\bord0\shad0\1c&H' + bgr(color)
                              + r'&\p1}m ' + f'{x:.1f} {y:.1f} l {x+w:.1f} {y:.1f} '
                              + f'{x+w:.1f} {y+h:.1f} {x:.1f} {y+h:.1f}' + r'{\p0}')

    def text(self, x, y, value, size=18, color=WHITE, bold=False, width=None):
        clip = '' if width is None else rf'\clip({int(x)},{int(y)},{int(x+width)},{int(y+size*1.5)})'
        self.lines.append(r'{\an7\pos(' + f'{x:.1f},{y:.1f}' + r')\fnDejaVu Sans\fs'
                          + str(size) + rf'\b{int(bold)}\bord0\shad0\q2\1c&H'
                          + bgr(color) + '&' + clip + '}' + literal(value))

    def wrap(self, x, y, value, width, size=18, count=2, color=WHITE, bold=False):
        lines = textwrap.wrap(clean(value), width=max(1, int(width / (size * .58))),
                              max_lines=count, placeholder='…')
        for row, line in enumerate(lines):
            self.text(x, y + row * (size + 5), line, size, color, bold, width)


def render(view):
    c = Canvas()
    px, py, pw, ph = PREVIEW
    # Four opaque regions instead of a full-screen rectangle over live video.
    c.rect(0, 0, WIDTH, py, BG)
    c.rect(0, py, px, ph, BG)
    c.rect(px+pw, py, WIDTH-px-pw, ph, BG)
    c.rect(0, py+ph, WIDTH, HEIGHT-py-ph, BG)
    c.rect(px-2, py-2, pw+4, 2, PREVIEW_LINE); c.rect(px-2, py+ph, pw+4, 2, PREVIEW_LINE)
    c.rect(px-2, py, 2, ph, PREVIEW_LINE); c.rect(px+pw, py, 2, ph, PREVIEW_LINE)
    c.text(36, 18, 'CHANNELBOX  /  TV GUIDE', 13, ACCENT, True)
    c.text(1040, 17, datetime.fromtimestamp(view['now']).strftime('%a, %b %d   %I:%M %p').replace(' 0', ' '), 13, MUTED, width=208)
    selected = view.get('selected')
    info = view.get('programme') or {}
    c.wrap(36, 62, info.get('title', 'Select a channel'), 810, 30, 2, bold=True)
    if selected:
        c.text(38, 145, selected.number + '  ·  ' + selected.name, 17, ACCENT, True, 805)
        if not info.get('missing') and info.get('start'):
            label = clock(info['start']) + ' – ' + clock(info['stop'])
            label += '    ·    ' + str(round((info['stop']-info['start'])/60)) + ' min'
            c.text(38, 177, label, 16, MUTED)
        details = info.get('description') or info.get('subtitle') or view.get('group', '')
        c.wrap(38, 208, details, 805, 17, 2, MUTED)
    state = 'UPCOMING  ·  OK tunes this channel live' if info.get('start', 0) > view['now'] else 'ON NOW  ·  OK to watch live'
    if info.get('missing'):
        state = 'SCHEDULE UNAVAILABLE  ·  Channel can still be watched'
    c.text(38, 265, view.get('notice') or state, 13, ACCENT, width=805)
    c.text(px, py+ph+12, 'WATCHING  ·  ' + view['current'].number + '  ' + view['current'].name, 13, MUTED, width=pw)
    c.rect(32, 294, 1216, 1, LINE)
    c.text(38, 300, 'YOUR LIBRARY', 11, MUTED, True)
    c.text(228, 300, 'CHANNEL', 11, MUTED, True)
    start = view['start']
    for i in range(4):
        x = GRID_X + i * GRID_W / 4
        c.text(x+7, 300, clock(start+i*HALF_HOUR), 13, ACCENT, True)
    for index, (key, label) in enumerate(FILTERS):
        y = GRID_Y + index * ROW_H
        active = key == view['filter']
        focused = view['focus'] == 'categories' and index == view['category']
        c.rect(32, y+2, 176, ROW_H-4, ACCENT if focused else ACTIVE if active else BG)
        c.text(44, y+8, label, 15, INK if focused else ACCENT if active else MUTED, active or focused, 153)
        c.text(44, y+29, f"{view['counts'].get(key, 0):,} channels", 10, INK if focused else ACCENT if active else MUTED)
    for row, item in enumerate(view['rows']):
        channel, programmes = item['channel'], item['programmes']
        y = GRID_Y + row * ROW_H
        selected_row = selected is not None and channel.number == selected.number
        c.rect(220, y+2, 186, ROW_H-4, SELECTED if selected_row else PANEL)
        c.text(228, y+15, channel.number, 15, ACCENT if selected_row else MUTED, True, 51)
        name = ('★ ' if item['favorite'] else '') + channel.name
        c.wrap(283, y+6, name, 115, 13, 2, WHITE, selected_row)
        for programme in programmes:
            begin = max(start, programme.get('visible_start', programme['start']))
            end = min(start+WINDOW, programme.get('visible_stop', programme['stop']))
            x, w = GRID_X+(begin-start)/WINDOW*GRID_W, (end-begin)/WINDOW*GRID_W
            active = selected_row and programme == info and view['focus'] == 'grid'
            c.rect(x+1, y+2, w-2, ROW_H-4, ACCENT if active else GRID if not programme.get('missing') else PANEL)
            if w > 28:
                c.text(x+9, y+14, programme['title'], 17 if active else 16,
                       INK if active else MUTED if programme.get('missing') else WHITE,
                       active, max(0, w-17))
    if not view['rows']:
        c.rect(220, GRID_Y+2, 1028, ROW_H*ROWS-4, PANEL)
        c.text(460, 420, 'No channels in this view yet', 24, MUTED)
        c.text(460, 460, 'Choose All channels, or add favorites with A.', 16, MUTED)
    if start <= view['now'] < start+WINDOW:
        x = GRID_X+(view['now']-start)/WINDOW*GRID_W
        c.rect(x, GRID_Y, 2, ROW_H*ROWS, ACCENT)
    c.rect(32, 640, 1216, 1, LINE)
    c.text(38, 654, '↑↓  Channels    ←→  Programmes / library    OK  Watch live    A  Favorite    C  Favorites', 15, WHITE)
    c.text(38, 685, 'H / Esc  Return to video     ·     B  Original guide', 12, MUTED)
    c.text(870, 685, f"{view['position']:,} / {view['total']:,} channels   ·   {datetime.fromtimestamp(start).strftime('%a, %b %d')}", 12, MUTED, width=378)
    return '\n'.join(c.lines)
