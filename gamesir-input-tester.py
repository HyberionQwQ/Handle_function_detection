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


def normalized(value, low, high):
    if high <= low or not all(math.isfinite(v) for v in (value, low, high)):
        return 0.0
    return max(-1.0, min(1.0, 2.0 * (value - low) / (high - low) - 1.0))


# Shared input backends: standalone copies from nav2_handshank_control.
"""Observe only the matching USB gamepad's screenshot macro through X11.

The gamepad emits Super+Print on its auxiliary keyboard interface. XInput can
observe that interface in the active desktop session without root permissions.
This optional diagnostic never grabs keys or sends controller commands.
"""
import os
from pathlib import Path
import pty
import re
import shutil
import subprocess


def usb_parent(node):
    path = (Path('/sys/class/input') / Path(node).name / 'device').resolve()
    for parent in (path, *path.parents):
        if (parent / 'idVendor').is_file() and (parent / 'idProduct').is_file():
            return parent
    return None


class ScreenshotInput:
    def __init__(self):
        self.process = None
        self.fd = None
        self.source = ''
        self.buffer = ''
        self.kind = None

    def start(self, gamepad_node):
        self.close()
        if not os.environ.get('DISPLAY') or not shutil.which('xinput'):
            return
        parent = usb_parent(gamepad_node)
        if parent is None:
            return
        try:
            listing = subprocess.check_output(['xinput', 'list', '--short'], text=True,
                                              stderr=subprocess.DEVNULL, timeout=.5)
            for line in listing.splitlines():
                match = re.match(r'^[\s⎜⎣⎡↳│]*Zikway HID gamepad Keyboard\s+id=(\d+).*slave\s+keyboard', line)
                if not match:
                    continue
                device = match.group(1)
                props = subprocess.check_output(['xinput', 'list-props', device], text=True,
                                                stderr=subprocess.DEVNULL, timeout=.5)
                node = re.search(r'Device Node \(\d+\):\s*"([^"]+)"', props)
                if not node or usb_parent(node.group(1)) != parent:
                    continue
                master, slave = pty.openpty()
                try:
                    self.process = subprocess.Popen(
                        ['xinput', 'test-xi2', '--root', device], stdout=slave,
                        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                except Exception:
                    os.close(master)
                    raise
                finally:
                    os.close(slave)
                self.fd = master
                os.set_blocking(master, False)
                self.source = node.group(1)
                return
        except (OSError, subprocess.SubprocessError):
            self.close()

    def feed(self, text):
        """Parse only raw PrintScreen events from the selected gamepad interface."""
        self.buffer += text
        events = []
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            line = line.strip()
            if line.startswith('EVENT type '):
                self.kind = True if '(RawKeyPress)' in line else (
                    False if '(RawKeyRelease)' in line else None)
            elif line == 'detail: 107' and self.kind is not None:
                # Measured X11 PrintScreen keycode (evdev KEY_SYSRQ 99 + 8).
                events.append(self.kind)
                self.kind = None
        if len(self.buffer) > 65536:
            self.buffer = ''
            self.kind = None
        return events

    def poll(self):
        if self.fd is None:
            return []
        events = []
        try:
            for _ in range(16):
                try:
                    data = os.read(self.fd, 4096)
                except BlockingIOError:
                    break
                if not data:
                    self.close()
                    break
                events.extend(self.feed(data.decode(errors='replace')))
        except OSError:
            self.close()
        return events

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        self.source = self.buffer = ''
        self.kind = None


"""Read-only Linux evdev access based on gamesir-input-tester.py."""
import fcntl
import os
from pathlib import Path
import struct
import time


EVENT = struct.Struct('@llHHi')
FACE_BUTTONS = {'A': 305, 'B': 304, 'X': 307, 'Y': 308}
AXES = (0, 1, 3, 4, 16, 17)
REQUIRED_AXES = (0, 1, 3)
SWITCH_AXES = {axis: axis for axis in AXES}
USB_AXES = {0: 0, 1: 1, 2: 3, 5: 4, 9: 9, 10: 10, 16: 16, 17: 17}
KNOWN_NAMES = ('Zikway HID gamepad', 'Pro Controller')


def find_device(name, text=None):
    if text is None:
        text = Path('/proc/bus/input/devices').read_text()
    # Prefer wired input at discovery; keep an existing connection until it drops.
    for candidate in KNOWN_NAMES if name == 'auto' else (name,):
        for block in text.split('\n\n'):
            if f'N: Name="{candidate}"' not in block.splitlines():
                continue
            for line in block.splitlines():
                if line.startswith('H: Handlers='):
                    for handler in line.split('=', 1)[1].split():
                        if handler.startswith('event'):
                            return '/dev/input/' + handler
    return None


class InputDevice:
    def __init__(self, device_name='auto', device_path=''):
        self.device_name, self.device_path = device_name, device_path
        self.detected_name = self.connection_type = ''
        self.profile = 'switch'
        self.axis_map = dict(SWITCH_AXES)
        self.button_map = {}
        self.raw_axes, self.raw_pressed = {}, set()
        self.screenshot = ScreenshotInput()
        self._screenshot_down = False
        self._screenshot_until = 0.0
        self.fd = None
        self.path = None
        self.identity = None
        self.axes, self.ranges, self.pressed = {}, {}, set()
        self.button_count = self.axis_count = self.dpad_event_count = 0
        self.direction_presses = dict.fromkeys(('up', 'down', 'left', 'right'), 0)
        self.last_event = None
        self.status = '正在寻找手柄（蓝牙 / USB）…' if device_name == 'auto' else f'正在寻找 {device_name}…'
        self._next_discovery = self._next_probe = 0.0
        self._dropped = False
        self._caught_up = True

    @property
    def ready(self):
        return self.fd is not None and not self._dropped and self._caught_up and all(
            axis in self.ranges and self.ranges[axis][1] > self.ranges[axis][0]
            for axis in REQUIRED_AXES)

    def normalized(self, axis):
        if self.fd is None or axis not in self.ranges or axis not in self.axes:
            return 0.0
        if self.profile == 'usb_hid' and axis in (0, 1, 3, 4):
            low, high = self.ranges[axis]
            # This HID reports integer center 128 for 0..255, not 127.5.
            # Zero the untouched axis even while the other axis is outside the circular deadzone.
            center = (low + high + 1) // 2
            if low < center < high:
                value = self.axes[axis]
                span = high - center if value >= center else center - low
                return max(-1.0, min(1.0, (value - center) / span))
        return normalized(self.axes[axis], *self.ranges[axis])

    def direction_states(self):
        x, y = self.axes.get(16, 0), self.axes.get(17, 0)
        return {'up': y < 0, 'down': y > 0, 'left': x < 0, 'right': x > 0}

    def disconnect(self, reason=None):
        self.screenshot.close()
        self._screenshot_down = False
        self._screenshot_until = 0.0
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = self.identity = None
        self.axes.clear()
        self.ranges.clear()
        self.pressed.clear()
        self.raw_axes.clear()
        self.raw_pressed.clear()
        self.detected_name = self.connection_type = ''
        self._dropped = False
        self._caught_up = True
        self.status = reason or ('未发现手柄（蓝牙 / USB），等待连接' if self.device_name == 'auto' else
                                 f'未发现 {self.device_name}，等待手柄连接')

    def configure_profile(self, name, bus):
        if name == 'Zikway HID gamepad' and bus == 0x03:
            self.profile = 'usb_hid'
            self.axis_map = dict(USB_AXES)
            # Physical USB A/B were measured as 304/305; keep the original UI IDs.
            self.button_map = {304: 305, 305: 304}
        elif name == 'Pro Controller' or (self.device_name != 'auto' and name == self.device_name):
            self.profile = 'switch'
            self.axis_map = dict(SWITCH_AXES)
            self.button_map = {}
        else:
            raise ValueError(f'不支持的手柄设备：{name}')
        self.detected_name = name
        self.connection_type = {0x03: 'USB', 0x05: '蓝牙'}.get(bus, '其它连接')

    def refresh_pressed(self):
        self.pressed = {self.button_map.get(i, i) for i in self.raw_pressed}
        if self._screenshot_down or time.monotonic() < self._screenshot_until:
            self.pressed.add(309)

    def process_screenshot_event(self, pressed):
        self._screenshot_down = pressed
        if pressed:
            # Keep press/release macros visible even within a single GUI refresh.
            self._screenshot_until = time.monotonic() + .2
        self.button_count += 1
        self.last_event = time.time()
        self.refresh_pressed()

    def resync(self):
        self.axes.clear()
        self.ranges.clear()
        self.pressed.clear()
        self.raw_axes.clear()
        self.raw_pressed.clear()
        for raw_axis, axis in self.axis_map.items():
            buf = bytearray(24)
            try:
                fcntl.ioctl(self.fd, 0x80184540 + raw_axis, buf, True)
                value, low, high, _, _, _ = struct.unpack('6i', buf)
                if high > low:
                    self.raw_axes[raw_axis] = value
                    self.axes[axis] = value
                    self.ranges[axis] = (low, high)
            except OSError:
                pass
        keys = bytearray(96)
        try:
            fcntl.ioctl(self.fd, 0x80604518, keys, True)  # EVIOCGKEY, 768 bits
            self.raw_pressed = {i for i in range(768) if keys[i // 8] & (1 << (i % 8))}
            self.refresh_pressed()
        except OSError:
            pass
        self._dropped = False
        if not all(axis in self.ranges for axis in REQUIRED_AXES):
            self.status = '设备缺少所需摇杆轴，速度保持为零'
        else:
            self.status = f'已连接：{self.detected_name} · {self.connection_type} · {self.path}'

    def connect(self, path):
        self.disconnect()
        self.path = path
        if path is None:
            return
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            self.identity = os.fstat(self.fd).st_ino
            name = bytearray(256)
            identity = bytearray(8)
            fcntl.ioctl(self.fd, 0x81004506, name, True)  # EVIOCGNAME
            fcntl.ioctl(self.fd, 0x80084502, identity, True)  # EVIOCGID
            self.configure_profile(name.split(b'\0', 1)[0].decode(errors='replace'),
                                   struct.unpack('4H', identity)[0])
            self.resync()
            if self.profile == 'usb_hid':
                self.screenshot.start(path)
        except (OSError, ValueError) as error:
            self.disconnect('无法读取手柄：' + str(error))

    def process_event(self, kind, code, value):
        if kind == 0 and code == 3:  # SYN_DROPPED: do not replay stale axis state
            self._dropped = True
            self.status = '输入队列丢帧，正在重新读取；速度保持为零'
            return
        if self._dropped:
            if kind == 0 and code == 0:
                self.resync()
            return
        if kind == 1:
            self.button_count += 1
            self.raw_pressed.add(code) if value else self.raw_pressed.discard(code)
            self.refresh_pressed()
        elif kind == 3:
            self.raw_axes[code] = value
            if code not in self.axis_map:
                return
            code = self.axis_map[code]
            previous = self.direction_states()
            self.axes[code] = value
            if code in (16, 17):
                self.dpad_event_count += 1
                for direction, active in self.direction_states().items():
                    if active and not previous[direction]:
                        self.direction_presses[direction] += 1
            else:
                self.axis_count += 1
        if kind in (1, 3):
            self.last_event = time.time()

    def poll(self):
        now = time.monotonic()
        if self.fd is None and now >= self._next_discovery:
            self._next_discovery = now + 1.0
            try:
                self.connect(self.device_path or find_device(self.device_name))
            except OSError as error:
                self.disconnect('设备发现失败：' + str(error))
        if self.fd is None:
            return
        try:
            if now >= self._next_probe:
                self._next_probe = now + 0.2
                if os.stat(self.path).st_ino != self.identity:
                    self.disconnect('手柄设备已更换，等待重新连接')
                    return
            for _ in range(32):
                try:
                    data = os.read(self.fd, EVENT.size * 128)
                except BlockingIOError:
                    self._caught_up = True
                    break
                if not data:
                    self.disconnect('手柄连接已断开')
                    break
                if len(data) % EVENT.size:
                    self.disconnect('收到不完整输入事件，等待重新连接')
                    break
                for offset in range(0, len(data), EVENT.size):
                    _, _, kind, code, value = EVENT.unpack_from(data, offset)
                    self.process_event(kind, code, value)
            else:
                self._caught_up = False
            if self.fd is not None:
                for pressed in self.screenshot.poll():
                    self.process_screenshot_event(pressed)
                if not self.screenshot.source:
                    self._screenshot_down = False
                self.refresh_pressed()
        except OSError as error:
            self.disconnect('连接中断：' + str(error))


class Tester(InputDevice):
    def __init__(self):
        super().__init__(device_name="auto")
        self.root = tk.Tk()
        self.root.title('盖世小鸡手柄 · Ubuntu 本地输入测试')
        self.root.geometry('820x840')
        self.root.configure(bg='#eef2f7')
        style = ttk.Style(self.root)
        style.configure('TFrame', background='#eef2f7')
        style.configure('TLabel', background='#eef2f7', foreground='#334155')
        self.last_save = 0
        self.deadzone_percent = tk.DoubleVar(value=2.0)
        self.status_text = tk.StringVar(value=self.status)
        self.detail = tk.StringVar(value='请拨动左右摇杆，并按几下 A/B 键。')
        ttk.Label(self.root, text='启明星 · 手柄测试', font=('', 20, 'bold')).pack(pady=(18, 8))
        ttk.Label(self.root, textvariable=self.status_text, font=('', 12)).pack()
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

    def processed_stick(self, xaxis, yaxis):
        if not self.ready:
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
        previous_event = self.last_event
        InputDevice.poll(self)
        self.status_text.set(self.status)
        if self.last_event != previous_event:
            self.detail.set('正在收到手柄输入')
        self.draw()
        if time.monotonic() - self.last_save > 1:
            data = {
                'connected': self.fd is not None, 'ready': self.ready, 'node': self.path,
                'device_name': self.detected_name, 'connection_type': self.connection_type,
                'input_profile': self.profile, 'raw_axes': self.raw_axes,
                'screenshot_source': self.screenshot.source,
                'raw_pressed_buttons': sorted(self.raw_pressed),
                'button_events': self.button_count, 'axis_events': self.axis_count,
                'dpad_events': self.dpad_event_count, 'directions': self.direction_states(),
                'direction_presses': self.direction_presses,
                'deadzone_percent': self.deadzone_percent.get(),
                'control_sticks': {'left': self.processed_stick(0, 1),
                                   'right': self.processed_stick(3, 4)},
                'pressed_buttons': sorted(self.pressed), 'face_button_mapping': FACE_BUTTONS,
                'axes': self.axes, 'last_event': self.last_event,
            }
            temporary = STAT_PATH.with_name(f'.{STAT_PATH.name}.{os.getpid()}.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.replace(STAT_PATH)
            self.last_save = time.monotonic()
        self.root.after(20, self.poll)

    def close(self):
        self.disconnect()
        self.root.destroy()


if __name__ == '__main__':
    Tester().root.mainloop()
