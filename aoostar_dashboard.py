#!/usr/bin/env python3
"""
AOOSTAR 前置屏 - 系统监控仪表盘（原厂风格：渐变彩色卡片 + 白色大字）
采集 CPU / 内存 / 磁盘 / 网络 / 温度，用 Pillow 渲染成 960x376 图片，
再调用项目自带 CLI 推送到 AOOSTAR 屏幕。

布局：2x2 渐变色块（橙 CPU / 蓝 MEMORY / 紫 DISK / 红 NETWORK），
白色描边数值框 + 白色大字，字体用项目自带的 HarmonyOS Sans SC Bold。
取数为 Linux 原生（psutil + sensors），不依赖 HWiNFO。

用法：
    python aoostar_dashboard.py once      # 采集并推送一帧
    python aoostar_dashboard.py preview    # 只生成本地预览图不推送
    python aoostar_dashboard.py daemon     # 常驻进程，每 N 秒刷新
"""
import os
import re
import sys
import time
import subprocess
import socket
import psutil
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ---------- 路径配置 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_SCRIPT = os.path.join(BASE_DIR, "aoostar_screen.py")
VENV_PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python")
PREVIEW_PATH = os.path.join(BASE_DIR, "dashboard_preview.png")
FRAME_PATH = "/tmp/aoostar_dashboard_frame.png"
FONT_HARMONY = os.path.join(BASE_DIR, "aoostar-x-compatible-data", "fonts", "HarmonyOS_Sans_SC_Bold.ttf")

# ---------- 屏幕参数 ----------
WIDTH, HEIGHT = 960, 376
REFRESH_SEC = 5

# ---------- 字体 ----------
def font(size):
    for p in (FONT_HARMONY,
              "/usr/share/fonts/google-noto-sans-cjk-vf-fonts/NotoSansCJK-VF.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

F_LABEL = font(26)
F_VALUE = font(88)
F_UNIT = font(36)
F_SUB = font(22)
F_BADGE = font(19)

WHITE = (255, 255, 255)
BG = (6, 10, 16)            # 屏幕底色：近黑

# 卡片渐变（上浅下深，模仿原厂高饱和色块）
C_CPU = ((255, 172, 64), (245, 108, 8))     # 橙
C_MEM = ((82, 152, 255), (28, 88, 216))     # 蓝
C_DISK = ((172, 116, 250), (108, 56, 206))  # 紫
C_NET = ((255, 132, 108), (238, 68, 56))    # 红橙


# ---------- 数据采集 ----------
def read_temp():
    """读取 CPU 温度，优先 lm-sensors，回退 thermal_zone。"""
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if "Tctl" in line or "Package id" in line or "Core 0" in line:
                m = re.search(r"\+?([\d.]+)\s*°?C", line)
                if m:
                    return float(m.group(1))
    except Exception:
        pass
    try:
        for z in sorted(os.listdir("/sys/class/thermal")):
            if z.startswith("thermal_zone") and "temp" in os.listdir(f"/sys/class/thermal/{z}"):
                with open(f"/sys/class/thermal/{z}/temp") as f:
                    v = int(f.read().strip()) / 1000.0
                if 0 < v < 120:
                    return v
    except Exception:
        pass
    return None


def get_disks():
    """按物理磁盘去重，返回最多两块盘的容量信息。

    同一物理盘可能有多个挂载点（如 / 和 /home 同属 nvme0n1），
    以设备名前缀归组，每组取容量最大的分区作代表，避免重复计数。
    """
    groups = {}
    pattern = re.compile(r"nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+")
    for p in psutil.disk_partitions(all=False):
        if p.fstype in ("squashfs", "tmpfs", "devtmpfs", "efivarfs") or "loop" in p.device:
            continue
        m = pattern.search(p.device)
        if not m:
            continue
        phys = m.group(0)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        if phys not in groups or u.total > groups[phys]["total"]:
            groups[phys] = {
                "name": phys,
                "mount": p.mountpoint,
                "total": u.total,
                "used": u.used,
                "pct": u.percent,
            }
    return sorted(groups.values(), key=lambda d: -d["total"])[:2]


_prev_net = None
_prev_time = None


def collect():
    global _prev_net, _prev_time
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    disks = get_disks()
    # 综合百分比 = 两盘已用之和 / 两盘容量之和
    total = sum(d["total"] for d in disks)
    used = sum(d["used"] for d in disks)
    disk_pct = (used / total * 100) if total else 0.0
    net = psutil.net_io_counters()
    now = time.time()

    up, down = 0.0, 0.0
    if _prev_net is not None and _prev_time:
        dt = now - _prev_time
        if dt > 0:
            up = (net.bytes_sent - _prev_net.bytes_sent) / dt
            down = (net.bytes_recv - _prev_net.bytes_recv) / dt
    _prev_net, _prev_time = net, now

    return {
        "cpu": cpu,
        "mem_pct": mem.percent,
        "mem_used": mem.used / (1024**3),
        "mem_total": mem.total / (1024**3),
        "disk_pct": disk_pct,
        "disks": disks,
        "net_up": up,
        "net_down": down,
        "temp": read_temp(),
        "time": datetime.now().strftime("%H:%M:%S"),
    }


# ---------- 绘图辅助 ----------
def blend(c1, c2, t):
    return tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))


def gradient_card(img, box, c1, c2, radius=22):
    """在指定区域画圆角渐变色块。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    grad = Image.new("RGB", (w, h))
    gd = ImageDraw.Draw(grad)
    for i in range(h):
        t = i / max(1, h - 1)
        gd.line((0, i, w, i), fill=blend(c1, c2, t))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    img.paste(grad, (x0, y0), mask)


def progress_bar(dr, box, pct, card_color):
    """进度条：半透明感的浅色槽 + 白色填充。"""
    x0, y0, x1, y1 = box
    dr.rounded_rectangle(box, radius=(y1 - y0) // 2,
                         fill=blend(card_color, WHITE, 0.38))
    fw = int((x1 - x0) * min(max(pct, 0), 100) / 100)
    if fw > 4:
        dr.rounded_rectangle((x0, y0, x0 + fw, y1), radius=(y1 - y0) // 2, fill=WHITE)


def value_frame(dr, box):
    """数值显示框：白色描边圆角矩形（模仿原厂）。"""
    dr.rounded_rectangle(box, radius=16, outline=WHITE, width=2)


# ---------- 主渲染 ----------
def render(d):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    dr = ImageDraw.Draw(img)

    margin, gap = 16, 14
    cw = (WIDTH - 2 * margin - gap) // 2
    ch = (HEIGHT - 2 * margin - gap) // 2
    xs = [margin, margin + cw + gap]
    ys = [margin, margin + ch + gap]

    cards = [
        (0, 0, "CPU", C_CPU, d["cpu"],
         f"TEMP {d['temp']:.0f}C" if d["temp"] is not None else "N/A"),
        (0, 1, "MEMORY", C_MEM, d["mem_pct"],
         f"{d['mem_used']:.1f}/{d['mem_total']:.0f} GB"),
    ]

    # 先画四张渐变卡片底色
    for row, col, label, colors, pct, sub in cards:
        x, y = xs[col], ys[row]
        gradient_card(img, (x, y, x + cw, y + ch), colors[0], colors[1])
    x, y = xs[0], ys[1]
    gradient_card(img, (x, y, x + cw, y + ch), C_DISK[0], C_DISK[1])
    x, y = xs[1], ys[1]
    gradient_card(img, (x, y, x + cw, y + ch), C_NET[0], C_NET[1])

    dr = ImageDraw.Draw(img)  # 底色画完后的绘制对象

    # 三张指标卡的文字与进度条
    for row, col, label, colors, pct, sub in cards:
        x, y = xs[col], ys[row]
        dr.text((x + 26, y + 14), label, font=F_LABEL, fill=WHITE)
        value_frame(dr, (x + 24, y + 50, x + 234, y + 148))
        num = f"{pct:.0f}"
        num_font = F_VALUE if len(num) <= 2 else font(76)
        nw = dr.textbbox((0, 0), num, font=num_font)[2]
        dr.text((x + 42, y + 52), num, font=num_font, fill=WHITE)
        dr.text((x + 52 + nw, y + 92), "%", font=F_UNIT, fill=WHITE)
        # 右侧副信息 + 进度条
        sw = dr.textbbox((0, 0), sub, font=F_SUB)[2]
        dr.text((x + cw - 26 - sw, y + 22), sub, font=F_SUB, fill=WHITE)
        progress_bar(dr, (x + 250, y + 78, x + cw - 26, y + 98), pct,
                     blend(colors[0], colors[1], 0.5))

    # DISK 卡：左侧综合百分比，右侧两条磁盘进度条
    x, y = xs[0], ys[1]
    dr.text((x + 26, y + 14), "DISK", font=F_LABEL, fill=WHITE)
    value_frame(dr, (x + 24, y + 50, x + 234, y + 148))
    num = f"{d['disk_pct']:.0f}"
    num_font = F_VALUE if len(num) <= 2 else font(76)
    nw = dr.textbbox((0, 0), num, font=num_font)[2]
    dr.text((x + 42, y + 52), num, font=num_font, fill=WHITE)
    dr.text((x + 52 + nw, y + 92), "%", font=F_UNIT, fill=WHITE)

    disks = d["disks"][:2]
    f_disk = F_SUB if len(disks) == 2 else F_LABEL
    for i, dk in enumerate(disks):
        by = y + 44 + i * 54
        # 标签：挂载点 + 已用/总容量
        label_txt = f"{dk['mount']}  {dk['used']/1024**3:.0f}/{dk['total']/1024**3:.0f}G"
        dr.text((x + 250, by), label_txt, font=f_disk, fill=WHITE)
        # 进度条（下方）
        progress_bar(dr, (x + 250, by + 28, x + cw - 26, by + 46), dk["pct"],
                     blend(C_DISK[0], C_DISK[1], 0.5))

    # NETWORK 卡：上下行并排大字
    x, y = xs[1], ys[1]
    dr.text((x + 26, y + 14), "NETWORK", font=F_LABEL, fill=WHITE)
    down = f"{d['net_down']/1024/1024:.1f}"
    up = f"{d['net_up']/1024/1024:.1f}"
    f_net = font(56)
    dr.text((x + 26, y + 58), "DOWN", font=F_BADGE, fill=WHITE)
    dr.text((x + 26, y + 80), down, font=f_net, fill=WHITE)
    dw = dr.textbbox((0, 0), down, font=f_net)[2]
    dr.text((x + 34 + dw, y + 100), "MB/s", font=F_BADGE, fill=WHITE)

    dr.text((x + 240, y + 58), "UP", font=F_BADGE, fill=WHITE)
    dr.text((x + 240, y + 80), up, font=f_net, fill=WHITE)
    uw = dr.textbbox((0, 0), up, font=f_net)[2]
    dr.text((x + 248 + uw, y + 100), "MB/s", font=F_BADGE, fill=WHITE)

    tw = dr.textbbox((0, 0), d["time"], font=F_BADGE)[2]
    dr.text((x + cw - 26 - tw, y + 20), d["time"], font=F_BADGE, fill=WHITE)

    return img


def push_to_screen(path):
    py = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable
    cmd = [py, CLI_SCRIPT, "image", path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print("PUSH FAILED:", res.stderr.strip(), file=sys.stderr)
    return res.returncode == 0


def frame(mode="once"):
    d = collect()
    img = render(d)
    if mode == "preview":
        img.save(PREVIEW_PATH)
        print("Preview saved:", PREVIEW_PATH)
        return True
    img.save(FRAME_PATH)
    print(f"[{d['time']}] CPU {d['cpu']:.0f}%  MEM {d['mem_pct']:.0f}%  "
          f"TEMP {d['temp'] if d['temp'] else 'N/A'}C")
    return push_to_screen(FRAME_PATH)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "daemon":
        interval = int(os.environ.get("REFRESH_SEC", REFRESH_SEC))
        print(f"Daemon mode, refresh every {interval}s")
        while True:
            try:
                frame("once")
            except Exception as e:
                print("Frame error:", e, file=sys.stderr)
            time.sleep(interval)
    elif mode in ("once", "preview"):
        ok = frame(mode)
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
