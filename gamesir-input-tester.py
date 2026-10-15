#!/usr/bin/env python3
"""Read-only controller tester using Linux evdev and the standard library."""
import fcntl
import json
import math
import os
from pathlib import Path
import struct
import time
import tkinter as tk
from tkinter import ttk

EVENT = struct.Struct('@llHHi')
STAT_PATH = Path('/tmp/gamesir-input-status.json')
# Physical labels confirmed on this GameSir controller in Switch mode.
FACE_BUTTONS = {'A': 305, 'B': 304, 'X': 307, 'Y': 308}


def apply_deadzone(x, y, deadzone):
    """Circular inner deadzone with continuous output and preserved direction."""
    magnitude = math.hypot(x, y)
    if magnitude <= deadzone or magnitude == 0:
        return 0.0, 0.0
    scale = (min(magnitude, 1.0) - deadzone) / ((1.0 - deadzone) * magnitude)
    return x * scale, y * scale


class Tester:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('盖世小鸡手柄 · Ubuntu 本地输入测试')
        self.root.geometry('820x840')
        self.root.configure(bg='#eef2f7')
        style = ttk.Style(self.root)
        style.configure('TFrame', background='#eef2f7')
        style.configure('TLabel', background='#eef2f7', foreground='#334155')
        self.fd = None
        self.node = None
        self.identity = None
        self.axes = {}
        self.ranges = {}
        self.pressed = set()
        self.button_count = self.axis_count = 0
        self.dpad_event_count = 0
        self.direction_presses = {direction: 0 for direction in ('up', 'down', 'left', 'right')}
        self.last_save = 0
        self.last_event = None
        self.deadzone_percent = tk.DoubleVar(value=2.0)
        self.status = tk.StringVar(value='正在寻找 Pro Controller…')
        self.detail = tk.StringVar(value='请拨动左右摇杆，并按几下 A/B 键。')
        ttk.Label(self.root, text='启明星 · 手柄测试', font=('', 20, 'bold')).pack(pady=(18, 8))
        ttk.Label(self.root, textvariable=self.status, font=('', 12)).pack()
        ttk.Label(self.root, textvariable=self.detail).pack(pady=8)
        deadzone_controls = ttk.Frame(self.root)
        deadzone_controls.pack()
        ttk.Label(deadzone_controls, text='中心死区').pack(side='left', padx=8)
        tk.Scale(deadzone_controls, variable=self.deadzone_percent, from_=0, to=15,
                 resolution=0.5, orient='horizontal', length=220,
                 showvalue=False, highlightthickness=0, bg='#eef2f7',
                 troughcolor='#dce3ee', activebackground='#c7d2fe').pack(side='left')
        self.deadzone_label = ttk.Label(deadzone_controls, text='2.0%', width=7)
        self.deadzone_label.pack(side='left', padx=8)
        ttk.Label(deadzone_controls, text='紫色圆点和控制值已应用死区').pack(side='left', padx=8)
        self.canvas = tk.Canvas(self.root, width=780, height=600, bg='#ffffff', highlightthickness=0)
        self.canvas.pack(padx=20, pady=8)
        self.counts = tk.StringVar()
        ttk.Label(self.root, textvariable=self.counts).pack(pady=10)
        ttk.Label(self.root, text='紫色圆点：控制位置    灰色圆点：原始位置    绿色：按键按下').pack()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(10, self.poll)

    def find_device(self):
        for block in Path('/proc/bus/input/devices').read_text().split('\n\n'):
            if 'N: Name="Pro Controller"' not in block:
                continue
            for line in block.splitlines():
                if line.startswith('H: Handlers='):
                    for item in line.split('=', 1)[1].split():
                        if item.startswith('event'):
                            return '/dev/input/' + item
        return None

    def disconnect(self):
        if self.fd is not None:
            os.close(self.fd)
        self.fd = None
        self.identity = None
        self.pressed.clear()
        self.axes[16] = self.axes[17] = 0

    def connect(self, node):
        self.disconnect()
        self.node = node
        if node is None:
            self.status.set('未发现手柄，请在蓝牙设置中连接 Pro Controller')
            return
        try:
            self.fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            self.identity = os.fstat(self.fd).st_ino
            self.axes.clear()
            self.ranges.clear()
            for axis in (0, 1, 3, 4, 16, 17):
                buf = bytearray(24)
                try:
                    fcntl.ioctl(self.fd, 0x80184540 + axis, buf, True)
                    value, low, high, _, _, _ = struct.unpack('6i', buf)
                    self.axes[axis] = value
                    self.ranges[axis] = (low, high)
                except OSError:
                    pass
            self.status.set('已连接：Pro Controller  ·  ' + node)
        except OSError as exc:
            self.disconnect()
            self.status.set('无法读取手柄：' + str(exc))

    def normalized(self, axis):
        low, high = self.ranges.get(axis, (-32767, 32767))
        if high <= low:
            return 0
        return max(-1, min(1, 2 * (self.axes.get(axis, 0) - low) / (high - low) - 1))

    def direction_states(self):
        x, y = self.axes.get(16, 0), self.axes.get(17, 0)
        return {'up': y < 0, 'down': y > 0, 'left': x < 0, 'right': x > 0}

    def processed_stick(self, xaxis, yaxis):
        if self.fd is None:
            return 0.0, 0.0
        return apply_deadzone(self.normalized(xaxis), self.normalized(yaxis),
                              self.deadzone_percent.get() / 100.0)

    def canvas_button(self, cx, cy, width, height, label, active=False,
                      circle=False, ink='#475569', tag='button'):
        c = self.canvas
        fill, outline = ('#bbf7d0', '#22c55e') if active else ('#ffffff', '#cbd5e1')
        bounds = (cx-width/2, cy-height/2, cx+width/2, cy+height/2)
        if circle:
            c.create_oval(*bounds, fill=fill, outline=outline, width=2, tags=tag)
        else:
            x0, y0, x1, y1 = bounds
            r = min(10, height / 3)
            c.create_polygon(x0+r, y0, x1-r, y0, x1, y0, x1, y0+r,
                             x1, y1-r, x1, y1, x1-r, y1, x0+r, y1,
                             x0, y1, x0, y1-r, x0, y0+r, x0, y0,
                             smooth=True, fill=fill, outline=outline,
                             width=2, tags=tag)
        c.create_text(cx, cy, text=label, fill='#166534' if active else ink,
                      font=('', 14 if circle and len(label) == 1 else 10, 'bold'), tags=tag)

    def draw(self):
        c = self.canvas
        c.delete('all')
        for cx, title, xaxis, yaxis, click_code, click_label in (
                (210, '左摇杆', 0, 1, 317, 'L3 · 左摇杆按下'),
                (570, '右摇杆', 3, 4, 318, 'R3 · 右摇杆按下')):
            cy, radius = 118, 80
            clicked = click_code in self.pressed
            c.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                          fill='#f0fdf4' if clicked else '#f8fafc',
                          outline='#22c55e' if clicked else '#94a3b8', width=2)
            c.create_line(cx-radius, cy, cx+radius, cy, fill='#cbd5e1')
            c.create_line(cx, cy-radius, cx, cy+radius, fill='#cbd5e1')
            deadzone_radius = radius * self.deadzone_percent.get() / 100.0
            c.create_oval(cx-deadzone_radius, cy-deadzone_radius,
                          cx+deadzone_radius, cy+deadzone_radius, outline='#f59e0b')
            raw_x = cx + self.normalized(xaxis) * radius
            raw_y = cy + self.normalized(yaxis) * radius
            c.create_oval(raw_x-3, raw_y-3, raw_x+3, raw_y+3, fill='#94a3b8', outline='')
            control_x, control_y = self.processed_stick(xaxis, yaxis)
            x = cx + control_x * radius
            y = cy + control_y * radius
            c.create_oval(x-8, y-8, x+8, y+8, fill='#4f46e5', outline='')
            c.create_text(cx, 18, text=title, font=('', 13))
            c.create_text(cx, 215, fill='#4338ca',
                          text=f'控制 X: {control_x:.4f}    Y: {control_y:.4f}')
            c.create_text(cx, 239, fill='#64748b',
                          text=f'原始 X: {self.axes.get(xaxis, 0)}    Y: {self.axes.get(yaxis, 0)}')
            self.canvas_button(cx, 273, 164, 27, click_label, clicked,
                               tag=f'button_{click_code}')
        self.deadzone_label.config(text=f'{self.deadzone_percent.get():.1f}%')
        c.create_rectangle(24, 303, 756, 581, fill='#f8fafc', outline='#e2e8f0')
        c.create_text(390, 319, text='启明星按键布局', fill='#64748b', font=('', 10))

        # Shoulder buttons on the left and right edges.
        for cx, cy, code, label in ((190, 326, 312, 'LT'), (190, 356, 310, 'LB'),
                                    (590, 326, 313, 'RT'), (590, 356, 311, 'RB')):
            self.canvas_button(cx, cy, 112, 24, label, code in self.pressed,
                               tag=f'button_{code}')

        # View and Menu flank Home; Screenshot is immediately below Home.
        for cx, cy, code, label, width, height, circle in (
                (334, 386, 314, 'View', 60, 28, False),
                (390, 365, 316, 'Home', 48, 48, True),
                (446, 386, 315, 'Menu', 60, 28, False),
                (390, 414, 309, '截图', 60, 26, False)):
            self.canvas_button(cx, cy, width, height, label, code in self.pressed,
                               circle=circle, tag=f'button_{code}')
        c.create_text(390, 455, text='GAMESIR', fill='#94a3b8', font=('', 13, 'bold'))
        c.create_text(390, 483, text='按住亮起 · 松开恢复', fill='#94a3b8', font=('', 9))

        # D-pad: four independent directions, including diagonal combinations.
        for direction, label, cx, cy in (
                ('up', '↑', 190, 401), ('left', '←', 143, 448),
                ('right', '→', 237, 448), ('down', '↓', 190, 495)):
            self.canvas_button(cx, cy, 40, 40, label,
                               self.direction_states()[direction], tag=f'dpad_{direction}')
        c.create_text(190, 448, text='方向键', fill='#64748b', font=('', 9))
        presses = self.direction_presses
        c.create_text(190, 545, text=f'↑ {presses["up"]}   ↓ {presses["down"]}   ← {presses["left"]}   → {presses["right"]}',
                      fill='#64748b', font=('', 10))

        # Physical GameSir print: Y above, A below, X left, B right.
        for label, cx, cy, ink in (
                ('Y', 590, 400, '#b88713'), ('X', 542, 448, '#3b6ec4'),
                ('B', 638, 448, '#ce5264'), ('A', 590, 496, '#128268')):
            code = FACE_BUTTONS[label]
            self.canvas_button(cx, cy, 46, 46, label, code in self.pressed,
                               circle=True, ink=ink, tag=f'button_{code}')
        c.create_text(590, 545, text='ABXY · 对应手柄印字', fill='#64748b', font=('', 10))
        self.counts.set(f'按键事件：{self.button_count}    摇杆事件：{self.axis_count}    方向键事件：{self.dpad_event_count}')

    def poll(self):
        node = self.find_device()
        try:
            inode = os.stat(node).st_ino if node else None
        except OSError:
            inode = None
        if node != self.node or inode != self.identity:
            self.connect(node)
        if self.fd is not None:
            try:
                for _ in range(32):
                    try:
                        data = os.read(self.fd, EVENT.size * 128)
                    except BlockingIOError:
                        break
                    if not data:
                        self.disconnect()
                        break
                    for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                        _, _, kind, code, value = EVENT.unpack_from(data, offset)
                        if kind == 1:
                            self.button_count += 1
                            self.pressed.add(code) if value else self.pressed.discard(code)
                        elif kind == 3:
                            previous_directions = self.direction_states()
                            self.axes[code] = value
                            if code in (16, 17):
                                self.dpad_event_count += 1
                                for direction, active in self.direction_states().items():
                                    if active and not previous_directions[direction]:
                                        self.direction_presses[direction] += 1
                            else:
                                self.axis_count += 1
                        if kind in (1, 3):
                            self.last_event = time.time()
                            self.detail.set('正在收到手柄输入')
            except OSError as exc:
                self.disconnect()
                self.status.set('连接中断：' + str(exc))
        self.draw()
        if time.monotonic() - self.last_save > 1:
            STAT_PATH.write_text(json.dumps({'connected': self.fd is not None, 'node': self.node,
                'button_events': self.button_count, 'axis_events': self.axis_count,
                'dpad_events': self.dpad_event_count, 'directions': self.direction_states(),
                'direction_presses': self.direction_presses,
                'deadzone_percent': self.deadzone_percent.get(),
                'control_sticks': {'left': self.processed_stick(0, 1),
                                   'right': self.processed_stick(3, 4)},
                'pressed_buttons': sorted(self.pressed), 'face_button_mapping': FACE_BUTTONS,
                'axes': self.axes, 'last_event': self.last_event}))
            self.last_save = time.monotonic()
        self.root.after(20, self.poll)

    def close(self):
        self.disconnect()
        self.root.destroy()


if __name__ == '__main__':
    Tester().root.mainloop()
