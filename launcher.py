import os, sys, socket, subprocess, time, webbrowser, threading, configparser, urllib.request, json, shutil, re, collections
import urllib.parse, ctypes
from ctypes import wintypes
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "DSH Dock"
DOCK_TITLE = "DSH Dock 管理界面"

def _resolve_base_dir():
    """定位 DSH 项目根目录：
    - 源码运行：launcher.py 所在目录
    - 打包运行（frozen）：exe 一般在 dist 下，向上找含 dsh.ini / DSH.ico 的目录，
      避免把 dist 当工作目录导致配置/日志/实例工作区错位
    """
    if getattr(sys, "frozen", False):
        start = os.path.dirname(os.path.abspath(sys.executable))
        cand = start
        for _ in range(3):
            if any(os.path.isfile(os.path.join(cand, n)) for n in ("dsh.ini", "DSH.ico")):
                return cand
            parent = os.path.dirname(cand)
            if parent == cand:
                break
            cand = parent
        return start
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _resolve_base_dir()
# 配置文件放在用户主目录 C:\Users\<用户名>\dsh.ini（各电脑自动落到本机对应位置），运行 exe 时自动生成
INI_FILE = os.path.join(os.path.expanduser("~"), "dsh.ini")
# 历史版本配置位置，按顺序作为迁移候选源（新位置不存在时才迁移）
LEGACY_INI_PATHS = [
    os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DSH-Dock", "dsh.ini"),
    os.path.join(BASE_DIR, "dsh.ini"),
]
LOG_FILE = os.path.join(BASE_DIR, "DSH.log")
LOG_MAX = 1024 * 1024  # 超过 1MB 轮转
REGISTRY_URL = "https://registry.npmjs.org/@deepseek-ai/dsh/latest"
DEFAULT_INI = """\
# DSH Dock 实例配置：每个 [instance:名称] 对应一个 dsh 实例（不同端口/工作区）
[instance:default]
port = 3080
workspace = {base}
auto_start = true
"""

# ---------- 日志（带轮转） ----------
def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            try:
                os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# ---------- 工具 ----------
GLOBAL_SECTION = "global"

def _ini_dsh_path():
    """读取 dsh.ini [global] dsh_path（用户手动指定/记录过的 dsh bin.js 路径）"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_FILE, encoding="utf-8-sig")
        if cp.has_section(GLOBAL_SECTION) and cp.has_option(GLOBAL_SECTION, "dsh_path"):
            p = cp.get(GLOBAL_SECTION, "dsh_path").strip()
            return p if os.path.isfile(p) else None
    except Exception:
        pass
    return None

def save_ini_dsh_path(bin_js):
    """把 dsh bin.js 路径写入 dsh.ini [global] dsh_path，供后续启动回退路由"""
    _ini_set("dsh_path", bin_js)
    log("已记录 dsh 路径: " + bin_js)

def get_startup_page():
    """托盘守护启动后自动打开的页面：admin=管理界面(默认) / default=default 实例工作页"""
    v = _ini_get("startup_page", "admin")
    return v if v in ("admin", "default") else "admin"

def set_startup_page(v):
    v = v if v in ("admin", "default") else "admin"
    _ini_set("startup_page", v)

def _ini_get(key, default=None):
    """读 dsh.ini [global] 段配置项"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_FILE, encoding="utf-8-sig")
        if cp.has_option(GLOBAL_SECTION, key):
            return cp.get(GLOBAL_SECTION, key).strip()
    except Exception:
        pass
    return default

def _ini_set(key, value):
    """写 dsh.ini [global] 段配置项（保留其余内容）"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_FILE, encoding="utf-8-sig")
        if not cp.has_section(GLOBAL_SECTION):
            cp.add_section(GLOBAL_SECTION)
        cp.set(GLOBAL_SECTION, key, str(value))
        with open(INI_FILE, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception as e:
        log("写配置失败 [global] %s: %s" % (key, e))

def resolve_dsh_path(p):
    """把用户提供的路径规整为 dsh 的 lib\\bin.js 路径；无效返回 None。
    支持直接给 bin.js 文件，或 dsh 安装目录 / npm 全局目录。"""
    p = (p or "").strip().strip('"').strip()
    if not p:
        return None
    if os.path.isfile(p) and os.path.basename(p).lower() == "bin.js":
        return os.path.abspath(p)
    if os.path.isdir(p):
        for cand in (os.path.join(p, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"),
                     os.path.join(p, "lib", "bin.js")):
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    return None

def is_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False

def port_pid(port):
    """返回监听指定端口的进程 PID（无则 None），用于停止外部实例"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":" + str(port)):
                return int(parts[4])
    except Exception:
        return None
    return None

def _me():
    return os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).lower()

def find_dsh():
    """返回 (node可执行, dsh bin.js 路径) 或 None。
    优先扫描 PATH/npm 全局目录；找不到时回退 dsh.ini [global] dsh_path 记录的路径。"""
    node = shutil.which("node")
    me = _me()
    dirs = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(os.path.join(appdata, "npm"))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and entry not in (".", os.curdir):
            dirs.append(entry)
    seen = set()
    bins = []
    for d in dirs:
        p = os.path.abspath(os.path.join(d, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"))
        if os.path.isfile(p) and p.lower() != me and p.lower() not in seen:
            seen.add(p.lower())
            bins.append(p)
    if not bins:
        cfg = _ini_dsh_path()
        if cfg:
            return (node, cfg) if node else None
    return (node, bins[0]) if node and bins else None

DSH = find_dsh()

# ---------- 实例 ----------
class Instance:
    def __init__(self, name, port, workspace, auto_start=True, env=None, dshhome=None):
        self.name = name
        self.port = int(port)
        self.workspace = workspace
        self.auto_start = auto_start
        self.env = env or {}          # 实例级环境变量（启动时注入子进程）
        self.dshhome = dshhome or None  # 自定义 DSH_HOME（None = 使用默认 ~/.dsh）
        self.proc = None       # 自己启动的 dsh 子进程
        self.stopping = False  # 手动停止标志（守护会尊重）
        self.external = False  # 端口被外部进程占用
        self.crashed = False   # 意外退出标志（由守护线程负责重启）
        self.last_rc = None
        self.start_ts = None   # 最近一次成功启动的时间（用于展示已运行时长）
        self._lock = threading.Lock()
        # 输出捕获：capture=True 时实例启动带 stdout 管道，用于解析 dsh web 的 LAN URL/token
        self.capture = False
        self.out_lines = collections.deque(maxlen=400)
        self.lan_url = None

    def status(self):
        if self.external:
            return "external"
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is None:
                return "running"
            # 进程已退出：记录退出码，若是意外退出则打上 crashed 标记交给守护处理
            self.proc = None
            self.last_rc = rc
            if not self.stopping:
                self.crashed = True
        return "stopped"

    def start(self):
        with self._lock:
            self._start_locked()

    def _start_locked(self):
        if self.status() != "stopped":
            return
        try:
            os.makedirs(self.workspace, exist_ok=True)
        except OSError:
            pass
        if is_open(self.port):
            self.external = True
            log(f"[{self.name}] 端口 {self.port} 已被外部进程占用，按外部实例处理")
            return
        if not DSH:
            log(f"[{self.name}] 找不到 dsh 命令，无法启动")
            return
        self.external = False
        self.stopping = False
        self.crashed = False
        node, bin_js = DSH
        cmd = [node, bin_js, "web", "--port", str(self.port), "--no-open"]
        env = os.environ.copy()
        for k, v in self.env.items():
            env[k] = v
        if self.dshhome:
            env["DSH_HOME"] = self.dshhome
        try:
            if self.capture:
                # 捕获模式：管道读 stdout，用于解析 dsh web 打印的 LAN URL/token
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             text=True, encoding="utf-8", errors="replace",
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                             cwd=self.workspace, env=env)
                threading.Thread(target=self._drain, daemon=True).start()
            else:
                self.proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=self.workspace, env=env)
        except Exception as e:
            log(f"[{self.name}] 启动失败: {e}")
            return
        self.start_ts = time.time()
        log(f"[{self.name}] 已启动 dsh (pid={self.proc.pid}, 端口 {self.port}, 工作区 {self.workspace})")

    def _drain(self):
        """捕获模式下的 stdout 读取线程：保留最近输出并解析 LAN URL/token"""
        try:
            for line in iter(self.proc.stdout.readline, ""):
                line = line.rstrip("\n")
                self.out_lines.append(line)
                if "dsh web:" in line:
                    m = re.search(r"LAN:\s*(http://[^\s)]+)\s*", line)
                    if m:
                        self.lan_url = m.group(1)
                    else:
                        m2 = re.search(r"http://[^\s)]+", line)
                        if m2:
                            self.lan_url = m2.group(0)
                    log(f"[{self.name}] web 地址: {self.lan_url}")
        except Exception as e:
            log(f"[{self.name}] 输出读取异常: {e}")

    def stop(self):
        with self._lock:
            if self.proc is not None:
                self.stopping = True
                pid = self.proc.pid
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
                except Exception as e:
                    log(f"[{self.name}] 停止出错: {e}")
                self.proc = None
                self.start_ts = None
                self.lan_url = None
                log(f"[{self.name}] 已停止")
            elif self.external:
                # 外部实例：按端口定位占用进程并强制结束，随后恢复对本实例的接管
                self.stopping = True
                pid = port_pid(self.port)
                if pid:
                    try:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
                        log(f"[{self.name}] 已按端口 {self.port} 结束外部进程 (pid={pid})")
                    except Exception as e:
                        log(f"[{self.name}] 停止外部实例出错: {e}")
                        return
                self.external = False
                self.proc = None
                self.start_ts = None

    def open_web(self):
        webbrowser.open(f"http://127.0.0.1:{self.port}")

    def monitor(self):
        while not stop_evt.is_set():
            try:
                if not self.stopping and self.crashed:
                    # 意外退出（手动停止的除外），一律守护重启（含手动开启的实例）
                    rc = self.last_rc
                    self.crashed = False
                    log(f"[{self.name}] dsh 意外退出 (rc={rc})，10 秒后自动重启")
                    update_ui()
                    time.sleep(10)
                    self.start()
                    update_ui()
                elif self.name == "default" and self.auto_start and not self.stopping and not is_open(self.port):
                    # 只主动拉起 default 实例；其他实例手动停止/退出后保持停止
                    if self.status() == "stopped":
                        self.start()
                        update_ui()
            except Exception as e:
                log(f"[{self.name}] 守护线程异常: {e}")
            time.sleep(3)

STATUS_TEXT = {"running": "运行中", "stopped": "已停止", "external": "外部运行"}

# ---------- 配置 ----------
INSTANCES = []

def _find_node_exe():
    """定位 node.exe：PATH 优先，再查常见安装路径（覆盖 PATH 未刷新的场景）"""
    exe = shutil.which("node")
    if exe:
        return exe
    cands = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe"),
        os.path.join(os.environ.get("ProgramW6432", ""), "nodejs", "node.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "node", "node.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""

def _find_npm_cmd():
    """定位 npm：PATH 或 nodejs 目录下的 npm.cmd"""
    c = shutil.which("npm")
    if c:
        return c
    ne = _find_node_exe()
    if ne:
        np = os.path.join(os.path.dirname(ne), "npm.cmd")
        if os.path.isfile(np):
            return np
    return ""

def _refresh_node_env():
    """把 nodejs 目录注入当前进程 PATH，让后续 which/npm 立即可见（winget 装完 PATH 不刷新）"""
    ne = _find_node_exe()
    if not ne:
        return
    d = os.path.dirname(ne)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if d and d not in parts:
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

def _assist_install_node(parent=None):
    """在用户允许的前提下自动安装 Node.js（含 npm）：优先 winget 静默安装，
    缺 winget 时弹提示引导手动下载。返回 True 表示 node/npm 已可用。"""
    from tkinter import messagebox
    if shutil.which("winget"):
        messagebox.showinfo("开始安装", "将用 winget 静默安装 Node.js（LTS），约 1-3 分钟，请稍候…", parent=parent)
        log("[bootstrap] 尝试 winget 安装 Node.js LTS")
        try:
            r = subprocess.run(
                ["winget", "install", "--id", "OpenJS.NodeJS.LTS", "--silent",
                 "--accept-source-agreements", "--accept-package-agreements",
                 "--disable-interactivity"],
                capture_output=True, text=True, timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _refresh_node_env()
            if _find_node_exe() and _find_npm_cmd():
                messagebox.showinfo("完成", "Node.js（含 npm）已安装。", parent=parent)
                log("[bootstrap] Node.js 安装成功")
                return True
            tail = ((r.stdout or "") + " " + (r.stderr or ""))[-300:]
            messagebox.showerror("安装失败", "winget 安装结束但未检测到 node。\n" + tail +
                                 "\n\n请到 nodejs.org 手动安装后重试。", parent=parent)
        except Exception as e:
            messagebox.showerror("安装异常", "winget 安装异常：%s\n\n请到 nodejs.org 手动安装。" % e, parent=parent)
    else:
        messagebox.showinfo("提示", "系统缺少 winget，无法自动安装 Node.js。\n\n"
                            "请到 nodejs.org 下载 LTS 版安装（自带 npm），装好后返回继续。", parent=parent)
    return False

def _assist_install_pnpm(parent=None):
    """询问是否协助安装 pnpm；允许则 npm install -g pnpm。返回 pnpm 是否可用"""
    from tkinter import messagebox
    if shutil.which("pnpm"):
        return True
    if not messagebox.askyesno("缺少 pnpm", "系统未找到 pnpm（安装插件需要）。\n\n"
                               "是否允许 DSH Dock 协助安装 pnpm？\n"
                               "（选「否」可稍后自行安装，不阻塞本次流程）",
                               default=messagebox.YES, parent=parent):
        log("[bootstrap] 用户拒绝协助安装 pnpm")
        return False
    npm_cmd = _find_npm_cmd()
    if not npm_cmd:
        messagebox.showerror("缺少 npm", "未找到 npm，无法安装 pnpm。", parent=parent)
        return False
    log("[bootstrap] 协助安装 pnpm")
    try:
        r = subprocess.run(["cmd", "/c", npm_cmd, "install", "-g", "pnpm"],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _refresh_node_env()
        if r.returncode == 0 and shutil.which("pnpm"):
            messagebox.showinfo("完成", "pnpm 已安装。", parent=parent)
            log("[bootstrap] pnpm 安装成功")
            return True
        tail = ((r.stdout or "") + " " + (r.stderr or ""))[-300:]
        messagebox.showerror("安装失败", "pnpm 安装失败：\n" + tail, parent=parent)
    except Exception as e:
        messagebox.showerror("安装异常", "pnpm 安装异常：%s" % e, parent=parent)
    return False

def bootstrap_dsh():
    """启动引导：新环境未安装 dsh 时弹窗询问，让用户选择：
    - 让 DSH Dock 协助安装（npm install -g @deepseek-ai/dsh）
    - 手动指定已安装的 dsh 路径
    成功返回 (node, bin_js)，取消/失败返回 None。"""
    import tkinter as tk
    from tkinter import messagebox, filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        while True:
            ans = messagebox.askyesnocancel(
                "DSH Dock 首次配置",
                "未检测到已安装的 dsh（DeepSeek Harness）。\n\n"
                "选「是」→ 由 DSH Dock 自动安装 dsh（需 node/npm，联网）\n"
                "选「否」→ 手动指定 dsh 安装路径\n"
                "选「取消」→ 退出",
                default=messagebox.YES, parent=root)
            if ans is True:  # 协助安装
                while not (_find_node_exe() and _find_npm_cmd()):
                    # 缺 node/npm：先问是否允许协助安装，拒绝则提示手动装后回到菜单
                    if messagebox.askyesno("缺少 Node.js",
                                           "未检测到 Node.js（内含 npm）。\n\n"
                                           "是否允许 DSH Dock 协助自动安装 Node.js（含 npm）？\n"
                                           "（选「否」→ 请自行到 nodejs.org 安装后重试）",
                                           default=messagebox.YES, parent=root):
                        if _assist_install_node(root):
                            break
                        # 安装失败：回到菜单顶部重新发起流程
                        continue
                    messagebox.showerror("缺少 Node.js", "未找到 node/npm，请先安装 Node.js（nodejs.org）后重试。",
                                         parent=root)
                    break
                if not (_find_node_exe() and _find_npm_cmd()):
                    continue
                _assist_install_pnpm(root)   # 缺 pnpm 时也在允许下协助安装（不阻塞）
                npm_cmd = _find_npm_cmd()
                ok = messagebox.askokcancel("自动安装", "将执行：\n\nnpm install -g @deepseek-ai/dsh@latest\n\n"
                                            "需要联网，可能耗时几分钟。确定继续？", parent=root)
                if not ok:
                    continue
                try:
                    r = subprocess.run(["cmd", "/c", npm_cmd, "install", "-g", "@deepseek-ai/dsh@latest"],
                                       capture_output=True, text=True, timeout=900,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception as e:
                    messagebox.showerror("安装异常", "安装过程异常：%s" % e, parent=root)
                    continue
                if r.returncode == 0:
                    new = find_dsh()
                    if new:
                        save_ini_dsh_path(new[1])
                        messagebox.showinfo("安装完成", "dsh 已安装并自动路由：\n%s" % new[1], parent=root)
                        return new
                    messagebox.showerror("未识别", "npm 安装结束，但未找到 dsh 可执行文件。"
                                            "请到命令行执行：npm install -g @deepseek-ai/dsh 检查。", parent=root)
                    continue
                tail = (((r.stdout or "") + " " + (r.stderr or ""))[-400:])
                messagebox.showerror("安装失败", "npm 安装失败：\n" + tail, parent=root)
            elif ans is False:  # 手动指定路径
                root.deiconify()
                root.update()
                p = filedialog.askopenfilename(
                    title="选择 dsh 的 lib\\bin.js 文件（或在取消后选择安装目录）",
                    filetypes=[("bin.js", "bin.js"), ("所有文件", "*.*")], parent=root) \
                    or filedialog.askdirectory(
                        title="或选择 dsh 安装目录（含 node_modules\\@deepseek-ai\\dsh）", parent=root)
                root.withdraw()
                if not p:
                    continue
                bin_js = resolve_dsh_path(p)
                if not bin_js:
                    messagebox.showerror("路径无效", "未能在该位置找到 dsh 的 lib\\bin.js，请重新选择。", parent=root)
                    continue
                if not shutil.which("node"):
                    messagebox.showerror("缺少 node", "找到了 dsh，但系统缺少 node 可执行文件，请先安装 Node.js。", parent=root)
                    continue
                save_ini_dsh_path(bin_js)
                messagebox.showinfo("完成", "dsh 路径已记录：\n%s" % bin_js, parent=root)
                return (shutil.which("node"), bin_js)
            else:  # 取消
                return None
    finally:
        root.destroy()

def _migrate_ini():
    """新位置不存在时，按历史版本顺序依次尝试迁移旧配置文件"""
    if os.path.exists(INI_FILE):
        return
    for src in LEGACY_INI_PATHS:
        if os.path.isfile(src):
            try:
                shutil.copy2(src, INI_FILE)
                log("已迁移旧配置文件 -> " + INI_FILE)
            except Exception:
                pass
            return

def load_config():
    _migrate_ini()
    if not os.path.exists(INI_FILE):
        with open(INI_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_INI.format(base=BASE_DIR))
        log("已生成默认配置文件 " + INI_FILE)
    cp = configparser.ConfigParser()
    cp.read(INI_FILE, encoding="utf-8-sig")
    for sec in cp.sections():
        if sec.startswith("instance:"):
            name = sec.split(":", 1)[1]
            port = cp.getint(sec, "port", fallback=3080)
            ws = cp.get(sec, "workspace", fallback=BASE_DIR)
            auto = cp.getboolean(sec, "auto_start", fallback=True)
            env = {}
            dshhome = None
            for k, v in cp.items(sec):
                if k.startswith("env."):
                    env[k[4:]] = v
                elif k == "dshhome":
                    dshhome = v.strip() or None
            INSTANCES.append(Instance(name, port, ws, auto, env, dshhome))
    if not INSTANCES:
        INSTANCES.append(Instance("default", 3080, BASE_DIR, True))
        log("配置中无实例，使用默认实例")

DSH_HOME_DIR = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")

def save_config():
    """把当前实例全量写回 dsh.ini（写前备份一份 .bak），保留 [global] 段原有配置"""
    if os.path.isfile(INI_FILE):
        try:
            shutil.copy2(INI_FILE, INI_FILE + ".bak")
        except OSError:
            pass
    old = configparser.ConfigParser()
    old.read(INI_FILE, encoding="utf-8-sig")
    cp = configparser.ConfigParser()
    # 先回写 [global] 等非 instance 段，避免手动指定的 dsh_path 被覆盖丢失
    for sec in old.sections():
        if not sec.startswith("instance:"):
            cp.add_section(sec)
            for k, v in old.items(sec):
                cp.set(sec, k, v)
    for i in INSTANCES:
        sec = "instance:" + i.name
        cp.add_section(sec)
        cp.set(sec, "port", str(i.port))
        cp.set(sec, "workspace", i.workspace)
        cp.set(sec, "auto_start", "true" if i.auto_start else "false")
        for k, v in i.env.items():
            cp.set(sec, "env." + k, v)
        if i.dshhome:
            cp.set(sec, "dshhome", i.dshhome)
    with open(INI_FILE, "w", encoding="utf-8") as f:
        cp.write(f)
    log("配置已保存到 " + INI_FILE)

def _validate_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, "端口必须是数字"
    if not (1 <= port <= 65535):
        return None, "端口必须在 1-65535 之间"
    if port == ADMIN_PORT:
        return None, "该端口被管理界面占用"
    for i in INSTANCES:
        if i.port == port:
            return None, f"端口 {port} 已被实例 {i.name} 使用"
    if is_open(port):
        return None, f"端口 {port} 已被其他程序占用"
    return port, None

def add_instance(name, port, workspace, dshhome=None):
    """新增实例。成功返回 None，失败返回错误信息。dshhome 可选：每实例专用 DSH_HOME，目录不存在会自动创建。"""
    if not name or not re.match(r"^[\w-]+$", name):
        return "实例名称只能包含字母、数字、_ 或 -"
    if _find_inst(name):
        return "同名实例已存在: " + name
    port, err = _validate_port(port)
    if err:
        return err
    ws = workspace.strip() or os.path.join(BASE_DIR, name)
    dh = (dshhome or "").strip() or None
    if dh and not os.path.isdir(dh):
        try:
            os.makedirs(dh)
            log(f"已自动创建专用 DSH_HOME 目录 {dh}")
        except OSError as e:
            return "无法创建 DSH_HOME 目录: " + str(e)
    INSTANCES.append(Instance(name, port, ws, auto_start=False, dshhome=dh))
    save_config()
    log(f"已新增实例 {name}（端口 {port}，工作区 {ws}，DSH_HOME={dh or '默认'}）")
    return None

def del_instance(name):
    """删除实例（default 不可删）。成功返回 None，失败返回错误信息"""
    if name == "default":
        return "default 实例不可删除"
    inst = _find_inst(name)
    if not inst:
        return "实例不存在: " + name
    if inst.status() in ("running", "external"):
        inst.stop()
    INSTANCES.remove(inst)
    save_config()
    log(f"已删除实例 {name}")
    return None

def set_port(name, port):
    """修改实例端口：运行中就停→改→保存→重启。成功返回 None"""
    inst = _find_inst(name)
    if not inst:
        return "实例不存在: " + name
    port, err = _validate_port(port)
    if err:
        return err
    if port == inst.port:
        return None
    was_running = inst.status() in ("running", "external")
    if was_running:
        inst.stop()
    inst.port = port
    save_config()
    if was_running:
        inst.start()
    log(f"实例 {name} 端口已改为 {port}")
    return None

def set_instance_config(inst, data):
    """更新实例的 DSH_HOME 与环境变量（data: {name, dshhome?, env?}）。返回错误信息或 None"""
    if "dshhome" in data:
        dshhome = (data.get("dshhome") or "").strip()
        if dshhome and not os.path.isdir(dshhome):
            try:
                os.makedirs(dshhome)
            except OSError as e:
                return "无法创建 DSH_HOME 目录: " + str(e)
        inst.dshhome = dshhome or None
    if "env" in data:
        if not isinstance(data["env"], dict):
            return "env 必须是键值对对象"
        new_env = {}
        for k, v in data["env"].items():
            k = str(k).strip()
            if not k:
                continue
            new_env[k] = str(v)
        inst.env = new_env
    save_config()
    log(f"[{inst.name}] 配置已更新 (DSH_HOME={inst.dshhome or '默认'}, env={len(inst.env)} 项)")
    return None

def read_credentials_keys():
    """轻量解析 .credentials.yaml 的 refs 段（避免依赖 yaml 库）"""
    keys = {}
    p = os.path.join(DSH_HOME_DIR, ".credentials.yaml")
    if os.path.isfile(p):
        try:
            for line in open(p, encoding="utf-8"):
                m = re.match(r"^\s{2}(\w+):\s*(\S+)", line)
                if m:
                    keys[m.group(1)] = m.group(2)
        except Exception:
            pass
    return keys

def parse_provider_refs():
    """从 settings.yaml 提取 provider 名 → apiKeyEnv 映射（llm-pi-ai.providers 段，flow 风格）"""
    p = os.path.join(DSH_HOME_DIR, "settings.yaml")
    if not os.path.isfile(p):
        return {}
    try:
        text = open(p, encoding="utf-8").read()
    except Exception:
        return {}
    idx = text.find("providers:")
    if idx < 0:
        return {}
    seg = []
    for line in text[idx + len("providers:"):].splitlines(keepends=True):
        if line.strip() and not line[0].isspace():
            break
        seg.append(line)
    seg = "".join(seg)
    out = {}
    for m in re.finditer(r"^\s*([\w-]+):\s*\{", seg, re.M):
        tail = seg[m.end():]
        am = re.search(r"apiKeyEnv:\s*(\w+)", tail[:2000])
        if am:
            out[am.group(1)] = m.group(1)
    return out

def list_credentials():
    """管理页用：全部凭据，掩码展示 + 来源标注（provider 反查）"""
    provider_map = parse_provider_refs()
    unknown = {"DEEPSEEK_API_KEY": "DeepSeek 官方"}
    out = []
    for name, val in read_credentials_keys().items():
        if not val:
            continue
        shown = val if len(val) <= 8 else val[:4] + "*" * 6 + val[-4:]
        label = provider_map.get(name) or unknown.get(name, "未关联 provider")
        out.append({"name": name, "masked": shown, "label": label,
                    "used": name in provider_map})
    return out

def _write_credentials_file(refs):
    """把 refs 原样写回 .credentials.yaml（写前自动备份 .bak）"""
    p = os.path.join(DSH_HOME_DIR, ".credentials.yaml")
    backup = p + ".bak"
    try:
        if os.path.isfile(p):
            shutil.copy2(p, backup)
        lines = ["version: 1", "refs:"]
        for k, v in refs.items():
            lines.append("  %s: %s" % (k, v))
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        return "写入凭据文件失败: " + str(e)
    return None

def set_credential(name, key):
    """新增/更新一个凭据（写前自动备份）"""
    name = (name or "").strip()
    key = (key or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_]{2,64}$", name):
        return "凭据名不合法（2-64 位字母/数字/下划线）"
    if not key or "\n" in key or ":" in key:
        return "Key 不能为空且不能含换行/冒号"
    refs = read_credentials_keys()
    refs[name] = key
    err = _write_credentials_file(refs)
    if err:
        return err
    log(f"[凭据] 已写入 {name}")
    return None

def del_credential(name):
    """删除一个凭据"""
    name = (name or "").strip()
    refs = read_credentials_keys()
    if name not in refs:
        return "不存在该凭据: " + name
    del refs[name]
    err = _write_credentials_file(refs)
    if err:
        return err
    log(f"[凭据] 已删除 {name}")
    return None

def deepseek_balance():
    """查 DeepSeek 官方账户余额（读 credentials.yaml 的 DEEPSEEK_API_KEY）"""
    key = read_credentials_keys().get("DEEPSEEK_API_KEY")
    if not key:
        return {"error": "未配置 DEEPSEEK_API_KEY"}
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        return {"error": "查询失败: " + str(e)}

def session_stats():
    """统计本机 dsh 会话记录（目录数 + 总体积）"""
    n = total = 0
    base = os.path.join(DSH_HOME_DIR, "sessions")
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            for f in files:
                if f == "session.jsonl.zstd":
                    n += 1
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    return {"sessions": n, "bytes": total}

# ---------- 插件市场与启停 ----------
PROFILE_DIR = os.path.join(DSH_HOME_DIR, "profiles", "web")
PKG_FILE = os.path.join(PROFILE_DIR, "package.json")
MARKET_URL = "https://dsh-plug.in/api/plugins.json"
MARKET_TTL = 3600  # 市场数据缓存 1 小时

_market_cache = {"ts": 0, "data": []}
_tasks = {}
_task_seq = 0

MARKET_SNAPSHOT = os.path.join(BASE_DIR, "_market_plugins.json")

def _norm_desc(d):
    """description 统一成 {zh, en}：兼容 dict / 数组[{language,content}] / 字符串"""
    out = {"zh": "", "en": ""}
    if isinstance(d, dict):
        out.update(d)
    elif isinstance(d, list):
        for it in d:
            if not isinstance(it, dict):
                continue
            lang = str(it.get("language") or "").lower()
            txt = str(it.get("content") or "")
            if lang.startswith("zh"):
                out["zh"] = txt
            elif lang.startswith("en"):
                out["en"] = txt
    elif d:
        out["en"] = str(d)
    out["zh"] = str(out.get("zh") or "")
    out["en"] = str(out.get("en") or "")
    return out

def _norm_item(x):
    """把市场条目归一化成统一字段（新 API / 旧快照都兼容）"""
    return {
        "name": x.get("name"),
        "id": x.get("id") or x.get("npm") or x.get("name"),
        "owner": x.get("owner") or "",
        "category": x.get("category") or "",
        "description": _norm_desc(x.get("description")),
        "stars": x.get("stars") or 0,
        "downloads": x.get("downloads") or 0,
        "added": x.get("added") or "",
        "install": x.get("install") or "",
    }

def _load_snapshot():
    """本地市场快照（旧格式 dump，条目全）；打包后从 exe 内资源读取"""
    path = MARKET_SNAPSHOT
    if not os.path.isfile(path) and getattr(sys, "frozen", False):
        alt = os.path.join(getattr(sys, "_MEIPASS", ""), "_market_plugins.json")
        if os.path.isfile(alt):
            path = alt
    if not os.path.isfile(path):
        return []
    try:
        doc = json.load(open(path, encoding="utf-8"))
        plugs = doc.get("plugins") if isinstance(doc, dict) else doc
        return [x for x in plugs if isinstance(x, dict)] if isinstance(plugs, list) else []
    except Exception:
        return []

def get_market(refresh=False):
    """拉取插件市场：实时 API（新格式）为增量，本地快照为主源，合并去重"""
    now = time.time()
    if not refresh and _market_cache["data"] and now - _market_cache["ts"] < MARKET_TTL:
        return _market_cache["data"], None
    live, err = [], None
    try:
        req = urllib.request.Request(MARKET_URL, headers={"User-Agent": "dsh-launcher"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.load(r)
            live = raw.get("plugins") if isinstance(raw, dict) else raw
            if not isinstance(live, list):
                live = []
    except Exception as e:
        err = "市场实时拉取失败: " + str(e)
    by_key = {}
    for it in _load_snapshot():
        n = _norm_item(it)
        by_key[str(n["id"]).lower()] = n
    for it in live:
        n = _norm_item(it)
        by_key.setdefault(str(n["id"]).lower(), n)
    data = sorted(by_key.values(), key=lambda x: -(x["downloads"] or 0))
    _market_cache.update({"ts": now, "data": data})
    return data, err

def _desc_text(d):
    """市场条目的 description 可能是 dict(zh/en) 或字符串，统一成纯文本"""
    if isinstance(d, dict):
        return " ".join(str(v) for v in d.values())
    return str(d or "")

_channels_cache = {}

def plugin_channels(name, npm, owner, install):
    """根据市场条目生成三通道安装 spec：
    - stable: npm latest（绿）
    - beta:   npm next（黄，仅当存在且 != latest）
    - alpha:  GitHub 最新（红，仅当 install/owner 可推出 github spec）
    查过的条目缓存，避免每次弹窗都请求 registry。"""
    key = "|".join([str(name), str(npm), str(owner)])
    if key in _channels_cache:
        return _channels_cache[key]
    out = {}
    gh = None
    m = re.search(r"github:([\w-]+/[\w.-]+)", install or "")
    if m:
        gh = m.group(1)
    elif owner and not npm:
        gh = "%s/%s" % (owner, name or "")
    if gh:
        out["alpha"] = {"label": "GitHub 最新", "spec": "github:" + gh, "color": "#e74c3c"}
    if npm:
        dist = None
        try:
            req = urllib.request.Request("https://registry.npmjs.org/" + urllib.parse.quote(npm),
                                         headers={"User-Agent": "dsh-launcher"})
            with urllib.request.urlopen(req, timeout=8) as r:
                dist = json.load(r).get("dist-tags") or {}
        except Exception:
            pass
        latest = (dist or {}).get("latest")
        out["stable"] = {"label": "稳定版" + (" " + latest if latest else ""),
                         "spec": npm, "color": "#2ecc71"}
        nxt = (dist or {}).get("next")
        if nxt and nxt != latest:
            out["beta"] = {"label": "测试版 " + nxt, "spec": npm + "@" + nxt, "color": "#f1c40f"}
    _channels_cache[key] = out
    return out

def installed_plugins():
    """解析 web profile 的 package.json：已装插件 + 启用状态"""
    if not os.path.isfile(PKG_FILE):
        return []
    try:
        pkg = json.load(open(PKG_FILE, encoding="utf-8"))
    except Exception:
        return []
    deps = pkg.get("dependencies") or {}
    bundles = (pkg.get("dsh", {}).get("profile", {}) or {}).get("bundles") or []
    out = []
    for name, spec in deps.items():
        out.append({"name": name, "spec": spec, "enabled": name in bundles})
    out.sort(key=lambda x: (not x["enabled"], x["name"]))
    return out

def set_plugin_enabled(name, enabled):
    """启停插件 = 增删 dsh.profile.bundles"""
    if not os.path.isfile(PKG_FILE):
        return "未找到 profile: web"
    pkg = json.load(open(PKG_FILE, encoding="utf-8"))
    deps = pkg.get("dependencies") or {}
    if name not in deps:
        return "未安装该插件: " + name
    bundles = (pkg.get("dsh", {}).get("profile", {}) or {}).get("bundles") or []
    if enabled and name not in bundles:
        bundles.append(name)
    elif not enabled and name in bundles:
        bundles.remove(name)
    pkg.setdefault("dsh", {}).setdefault("profile", {})["bundles"] = bundles
    with open(PKG_FILE, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)
    log(f"[插件] {'启用' if enabled else '禁用'} {name}")
    return None

def plugin_install(spec):
    """异步安装插件（后台线程跑 dsh CLI），返回任务 id"""
    global _task_seq
    _task_seq += 1
    task_id = "T%03d" % _task_seq
    info = {"id": task_id, "spec": spec, "status": "running", "log": [], "ts": time.time()}
    _tasks[task_id] = info
    threading.Thread(target=_plugin_worker, args=(info,), daemon=True).start()
    return task_id

def _plugin_worker(info):
    node, bin_js = DSH
    cmd = [node, bin_js, "plugin", "--profile", "web", "add"] + info["spec"].split()
    info["log"].append("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                info["log"].append(line)
        proc.wait()
        info["status"] = "done" if proc.returncode == 0 else "error"
        if proc.returncode != 0:
            info["log"].append(f"(exit={proc.returncode})")
    except Exception as e:
        info["status"] = "error"
        info["log"].append("异常: " + str(e))

# ---------- 管理界面（HTTP 服务：一个网页管理全部实例） ----------
ADMIN_PORT = int(os.environ.get("DSH_ADMIN_PORT", "3999"))

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>DSH Dock 管理界面</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#0f1222;color:#e8eaf6;padding:24px}
h1{font-size:20px;margin-bottom:16px}
.bar{display:flex;gap:12px;margin-bottom:16px}
button{padding:8px 14px;border:none;border-radius:8px;background:#4d6bfe;color:#fff;cursor:pointer;font-size:14px}
button.ghost{background:#2a2f45}
button:hover{opacity:.85}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.card{background:#1a2038;border:1px solid #2a3050;border-radius:12px;padding:16px}
.head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.dot{width:10px;height:10px;border-radius:50%}
.dot.on{background:#2ecc71}.dot.off{background:#e74c3c}.dot.ext{background:#f39c12}
.name{font-weight:600}
.port{color:#8f9bd0;font-size:12px;margin-left:auto}
.ws{color:#8f9bd0;font-size:12px;margin-bottom:12px;word-break:break-all}
.ops{display:flex;gap:8px;flex-wrap:wrap}
.ops button{flex:1 1 auto;min-width:56px}
.ops button.danger{background:#6b2330}
button.danger{background:#6b2330}
.tabs{display:flex;gap:10px;margin-bottom:20px;border-bottom:2px solid #2a3050;padding-bottom:10px}
.tabs button{background:transparent;border:1px solid transparent;border-radius:8px;padding:8px 20px;color:#8f9bd0;font-size:15px}
.tabs button.on{background:#4d6bfe;color:#fff}
.panel{display:none}.panel.on{display:block}
.plug-bar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.plug-bar input[type=text]{padding:8px 12px;border-radius:8px;border:1px solid #2a3050;background:#1a2038;color:#e8eaf6;font-size:14px;width:300px;outline:none}
.plug-card{background:#1a2038;border:1px solid #2a3050;border-radius:12px;padding:14px}
.pname{font-weight:600;word-break:break-all}
.pmeta{color:#8f9bd0;font-size:12px;margin:4px 0;word-break:break-all}
.pdesc{color:#aab2d8;font-size:13px;margin:6px 0}
.pops{display:flex;gap:8px;align-items:center}
.switch{position:relative;width:40px;height:20px;background:#3a4166;border-radius:10px;cursor:pointer;flex:none}
.switch.on{background:#2ecc71}
.switch::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:left .15s}
.switch.on::after{left:22px}
.task-box{background:#0d1128;border:1px solid #2a3050;border-radius:10px;padding:10px;margin-top:8px;max-height:200px;overflow:auto;font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;color:#9fd7a5}
.badge{background:#3a4166;border-radius:6px;padding:1px 8px;font-size:12px}
.badge.on{background:#23643c}.badge.err{background:#7a2830}
hr.sep{border:none;border-top:1px solid #2a3050;margin:18px 0}
</style>
</head>
<body>
<div class="tabs">
<button id="tabbtn-inst" class="on" onclick="showTab('inst')">实例管理</button>
<button id="tabbtn-plug" onclick="showTab('plug')">插件</button>
<button id="tabbtn-api" onclick="showTab('api')">API</button>
<button id="tabbtn-cron" onclick="showTab('cron')">定时任务</button>
<button id="tabbtn-set" onclick="showTab('set')">设置</button>
</div>
<div id="panel-inst" class="panel on">
<h1>DSH Dock 实例管理</h1>
<div class="bar">
<button onclick="mobileConn()">手机连接</button>
<button onclick="api('/api/allstart')">全部启动</button>
<button class="ghost" onclick="api('/api/allstop')">全部停止</button>
<button class="ghost" onclick="refresh()">刷新</button>
<button class="ghost" onclick="addInst()">＋ 新增实例</button>
</div>
<div class="cards" id="cards"></div>
<div class="head" style="margin-top:20px"><b>用量概览</b><span style="flex:1"></span><button class="ghost" onclick="loadUsage()">刷新用量</button></div>
<div class="cards"><div class="card" id="usageCard">加载中…</div></div>
</div>

<div id="panel-plug" class="panel">
  <div style="display:flex;gap:18px;align-items:flex-start">
    <div style="flex:1;min-width:0">
      <div class="plug-bar">
        <b>已装插件</b><span class="badge" id="plugCount"></span>
        <span style="flex:1"></span>
        <button class="ghost" onclick="setAllPlugins(true)">全部启用</button>
        <button class="ghost" onclick="setAllPlugins(false)">全部禁用</button>
      </div>
      <div class="cards" id="plugCards"></div>
      <hr class="sep">
      <div class="plug-bar">
        <b>插件市场</b>
        <input type="text" id="mktQ" placeholder="搜索插件名 / 作者 / 描述…" oninput="loadMarket()">
        <button class="ghost" onclick="loadMarket(true)">刷新市场</button>
        <span class="badge" id="mktCount"></span>
      </div>
      <div class="cards" id="mktCards"></div>
    </div>
    <div style="width:360px;flex:none;position:sticky;top:16px">
      <div class="plug-bar">
        <b>安装日志</b><span class="badge" id="taskCount"></span>
        <span style="flex:1"></span>
        <button class="ghost" onclick="loadTasks()">刷新</button>
      </div>
      <div id="taskList" style="display:flex;flex-direction:column;gap:8px;overflow:auto"></div>
    </div>
  </div>
</div>

<div id="panel-api" class="panel">
  <h1>API 凭据管理</h1>
  <p style="color:#8f9bd0;font-size:13px">读写 <code style="color:#9fd7a5">~/.dsh/.credentials.yaml</code>，改动前自动备份 .bak，保存后重启 dsh 实例生效。</p>
  <div class="plug-bar">
    <input type="text" id="credName" placeholder="凭据名，如 OPENCODE_GO_API_KEY" style="width:260px">
    <input type="text" id="credKey" placeholder="粘贴 API Key" style="width:320px" spellcheck="false">
    <button onclick="credSave()">保存 / 更新</button>
    <button class="ghost" style="margin-left:auto" onclick="loadCreds()">刷新</button>
  </div>
  <div class="cards" id="credCards"></div>
</div>

<div id="panel-set" class="panel">
  <h1>设置</h1>
  <div class="cards">
    <div class="card">
      <div class="head"><span class="name">开机自启</span>
        <span class="switch" id="autoSw" onclick="toggleAuto()" style="margin-left:auto"></span>
      </div>
      <div class="ws">注册 <code style="color:#9fd7a5">HKCU\...\Run</code>，登录 Windows 后自动启动托盘守护。（也可以在托盘菜单里开关）</div>
      <div class="pops"><span class="badge" id="autoState"></span></div>
    </div>
    <div class="card">
      <div class="head"><span class="name">启动后打开</span>
        <select id="startupPageSel" style="margin-left:8px;flex:1;max-width:260px" onchange="saveStartupPage()">
          <option value="admin">管理界面 (3999)</option>
          <option value="default">default 工作页 (3080)</option>
        </select>
      </div>
      <div class="ws">托盘守护启动后自动打开的页面。选 default 工作页时，会等网页就绪再打开浏览器，未连通会自动重试打开。</div>
    </div>
    <div class="card">
      <div class="head"><span class="name">dsh 本体更新</span>
        <select id="dshCh" style="margin-left:8px" onchange="checkDsh()">
          <option value="stable">稳定版</option>
          <option value="beta">测试版</option>
          <option value="alpha">内测版</option>
        </select>
        <button id="dshUpdBtn" style="margin-left:auto" onclick="checkDsh()">检查更新</button>
      </div>
      <div class="ws">选择通道检查 dsh 版本；更新前会自动探测插件兼容性（不兼容会弹窗确认）。升级后需重启实例生效。</div>
      <div class="task-box" id="dshUpdBox" style="margin-top:8px">点击「检查更新」查看版本状态</div>
    </div>
    <div class="card">
      <div class="head"><span class="name">dsh-market 插件更新</span>
        <button id="mktUpdBtn" style="margin-left:auto" onclick="checkMkt()">检查更新</button>
      </div>
      <div class="ws">检查插件市场模块（dshmarket）是否有新版本；更新后需重启实例生效。</div>
      <div class="task-box" id="mktUpdBox" style="margin-top:8px">点击「检查更新」查看版本状态</div>
    </div>
    <div class="card">
      <div class="head"><span class="name">插件兼容性</span>
        <select id="plugCh" style="margin-left:8px">
          <option value="current">当前内核</option>
          <option value="stable">稳定版</option>
          <option value="beta">测试版</option>
          <option value="alpha">内测版</option>
        </select>
        <button id="plugFixBtn" style="margin-left:auto" onclick="plugFix()">一键修复</button>
        <button style="margin-left:8px" onclick="plugCompat()">检测</button>
      </div>
      <div class="ws">检查已装插件声明的 dsh-tools 版本范围与内核是否匹配，防止工具调度报错（prepare）；「一键修复」会重建 dsh-tools junction 指向全局副本。</div>
      <div class="task-box" id="plugBox" style="margin-top:8px">点击「检测」查看插件兼容状态</div>
    </div>
    <div class="card">
      <div class="head"><span class="name">环境诊断（doctor）</span>
        <button style="margin-left:auto" onclick="runDoctor()">运行诊断</button>
      </div>
      <div class="ws">检查 Node / npm / pnpm / dsh 安装、profile、端口、凭据、日志，输出脱敏报告。</div>
      <div class="task-box" id="doctorBox" style="margin-top:8px">点击「运行诊断」查看报告</div>
    </div>
  </div>
</div>

<div id="panel-cron" class="panel">
  <h1>定时任务</h1>
  <p style="color:#8f9bd0;font-size:13px">到点自动启动任务指定的 dsh 端口与工作目录，把任务需求发给 dsh 执行，全程监控进度，完成后弹出桌面提醒。</p>
  <div class="plug-bar">
    <b>任务列表</b><span class="badge" id="cronCount"></span>
    <span style="flex:1"></span>
    <button onclick="cronNew()">＋ 新增任务</button>
    <button class="ghost" onclick="loadCron()">刷新</button>
  </div>
  <div class="cards" id="cronCards">加载中…</div>
  <div style="margin-top:16px">
    <div class="head"><b>运行记录</b><span style="flex:1"></span></div>
    <div class="task-box" id="cronLogBox" style="max-height:300px">选中任务后在这里查看实时输出</div>
  </div>
</div>

<!-- 新增/编辑定时任务弹层 -->
<div id="cronBox" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:50;align-items:flex-start;justify-content:center;padding:30px 0;overflow:auto">
  <div style="background:#1a2038;border:1px solid #3a4166;border-radius:14px;padding:22px;width:640px;max-width:94vw">
    <div class="head"><b id="cronBoxTitle">新增定时任务</b><span style="flex:1"></span><button class="ghost" onclick="cronClose()">关闭</button></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:14px 0">
      <div><div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">任务名称 *</div><input id="cronName" style="width:100%" placeholder="如：早间素材整理"></div>
      <div><div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">目标实例 *</div><select id="cronInst" style="width:100%"></select></div>
    </div>
    <div style="margin:12px 0">
      <div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">调度形式 *</div>
      <select id="cronSched" style="width:100%" onchange="cronSchedUI()">
        <option value="once">一次性 · 指定时间执行一次</option>
        <option value="interval">周期 · 每隔 N 分钟执行</option>
        <option value="daily">每天 · 固定时刻执行</option>
        <option value="weekly">每周 · 固定星期与时刻执行</option>
      </select>
    </div>
    <div id="cronParamLine" style="margin:12px 0;display:none"></div>
    <div style="margin:12px 0">
      <div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">工作目录（留空 = 实例默认工作区）</div>
      <input id="cronWs" style="width:100%" placeholder="如：D:\work\weekly-report">
    </div>
    <div style="margin:12px 0">
      <div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">任务需求（发给 dsh 的描述）*</div>
      <textarea id="cronDesc" rows="5" style="width:100%;resize:vertical;padding:8px 12px;border-radius:8px;border:1px solid #2a3050;background:#0d1128;color:#e8eaf6;font-size:13px;outline:none;font-family:inherit" placeholder="如：把 D:\data 里的周报数据汇总成一份 Markdown 报告，输出到 report.md"></textarea>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px">
      <button class="ghost" onclick="cronClose()">取消</button>
      <button onclick="cronSave(false)">保存</button>
      <button onclick="cronSave(true)">保存并立即执行</button>
    </div>
  </div>
</div>

<script>
const STATUS={running:"运行中",stopped:"已停止",external:"外部运行"};
async function api(p){const r=await fetch(p,{method:'POST'});try{const j=await r.json();if(j&&j.ok===false&&j.error)alert('失败: '+j.error);}catch(e){}refresh();}
async function refresh(){
 const r=await fetch('/api/status');const list=await r.json();
 const c=document.getElementById('cards');c.innerHTML='';
 for(const it of list){
  const cls=it.status==='running'?'on':(it.status==='external'?'ext':'off');
  const op1=it.status==='running'||it.status==='external'
   ?`<button onclick="api('/api/stop?name=${it.name}')">停止</button>`
   :`<button onclick="api('/api/start?name=${it.name}')">启动</button>`;
  const uptime=it.uptime?(' · 已运行 '+fmtUptime(it.uptime)):'';
  c.innerHTML+=`<div class="card"><div class="head"><span class="dot ${cls}"></span><span class="name">${it.name}</span><span class="port" style="cursor:pointer" title="点击修改端口" onclick="editPort('${it.name}',${it.port})">:${it.port} ✎</span></div><div class="ws">${it.workspace}${it.dshhome?('<br>DSH_HOME: '+it.dshhome):''}${it.env&&Object.keys(it.env).length?('<br>env: '+JSON.stringify(it.env)):''}${uptime}</div><div class="ops">${op1}<button onclick="api('/api/open?name=${it.name}')">打开</button><button onclick="embed('${it.name}')">内嵌</button><button onclick="tui('${it.name}')">TUI</button><button onclick="cfgInst('${it.name}')">配置</button>${it.name==='default'?'':`<button class="ghost danger" onclick="delInst('${it.name}')">删除</button>`}</div></div>`;
 }
}
function fmtUptime(s){s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}小时${m}分`:(m?m+'分':'刚启动');}
async function addInst(){
 const name=prompt('实例名称（仅用字母、数字、-_）:','work');if(!name)return;
 const port=prompt('端口号:','8080');if(!port)return;
 const ws=prompt('工作区路径（留空 = 与主程序同目录）:','')||'';
 const dh=prompt('专用 DSH_HOME 路径（留空 = 共用默认 ~\\.dsh，不存在会自动创建）:','')||'';
 await api('/api/add?name='+encodeURIComponent(name)+'&port='+encodeURIComponent(port)+'&workspace='+encodeURIComponent(ws)+'&dshhome='+encodeURIComponent(dh));
}
async function editPort(name,old){
 const p=prompt('新的端口号:',old);if(!p)return;
 await api('/api/setport?name='+encodeURIComponent(name)+'&port='+encodeURIComponent(p));
}
async function delInst(name){
 if(!confirm('确定删除实例「'+name+'」？若正在运行会先停止。'))return;
 await api('/api/del?name='+encodeURIComponent(name));
}
function embed(name){fetch('/api/embed?name='+encodeURIComponent(name),{method:'POST'}).then(async r=>{try{const j=await r.json();if(j&&j.ok===false)alert('内嵌窗口失败: '+(j.error||'未知错误'));}catch(e){}}).catch(e=>alert('内嵌请求失败: '+e));}
function tui(name){fetch('/api/tui?name='+encodeURIComponent(name),{method:'POST'}).then(async r=>{try{const j=await r.json();if(j&&j.ok===false)alert('TUI 启动失败: '+(j.error||'未知错误'));}catch(e){}}).catch(e=>alert('TUI 请求失败: '+e));}
function cfgInst(name){
 const dh=prompt('DSH_HOME 路径（留空 = 默认 ~\\.dsh，不存在会自动创建）：','');if(dh===null)return;
 const envs=prompt('环境变量（每行 KEY=VALUE，留空该行即删除）：','');
 if(envs===null)return;
 const env={};
 if(envs&&envs.trim()){for(const line of envs.split(String.fromCharCode(10))){const m=line.trim().match(/^([^=]+)=(.*)$/);if(m)env[m[1].trim()]=m[2].trim();}}
 fetch('/api/setenv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,dshhome:dh,env})})
  .then(r=>r.json()).then(j=>{if(j&&j.ok===false)alert('失败: '+j.error);else{alert('已保存（重启实例后生效）');refresh();}});
}
async function loadUsage(){
 const el=document.getElementById('usageCard');el.textContent='统计中…';
 try{
  const r=await fetch('/api/usage');const u=await r.json();
  const bal=u.balance&&!u.balance.error&&u.balance.balance_infos
   ?u.balance.balance_infos.map(b=>`${b.currency}: 总余额 ¥${b.total_balance}（充值 ¥${b.topped_up_balance} + 赠送 ¥${b.granted_balance}）`).join('<br>')
   :`<span style="color:#f39c12">余额查询不可用：${(u.balance&&u.balance.error)||'无 key'}</span>`;
  const s=u.sessions;
  el.innerHTML=`<b>DeepSeek 账户余额</b><br>${bal}<br><br><b>本地会话记录</b><br>${s.sessions} 个会话目录 · 共 ${(s.bytes/1024/1024).toFixed(1)} MB`;
 }catch(e){el.textContent='用量加载失败: '+e;}
}
function showTab(t){
 document.getElementById('panel-inst').className='panel'+(t==='inst'?' on':'');
 document.getElementById('panel-plug').className='panel'+(t==='plug'?' on':'');
 document.getElementById('panel-api').className='panel'+(t==='api'?' on':'');
 document.getElementById('panel-cron').className='panel'+(t==='cron'?' on':'');
 document.getElementById('panel-set').className='panel'+(t==='set'?' on':'');
 document.getElementById('tabbtn-inst').className=t==='inst'?'on':'';
 document.getElementById('tabbtn-plug').className=t==='plug'?'on':'';
 document.getElementById('tabbtn-api').className=t==='api'?'on':'';
 document.getElementById('tabbtn-cron').className=t==='cron'?'on':'';
 document.getElementById('tabbtn-set').className=t==='set'?'on':'';
 if(t==='plug'){loadPlugins();loadMarket();loadTasks();}
 if(t==='api'){loadCreds();}
 if(t==='cron'){loadCron();fillCronInsts();}
 if(t==='set'){loadAuto();loadStartupPage();}
}
async function loadStartupPage(){
 try{
  const r=await fetch('/api/startuppage');const j=await r.json();
  const sel=document.getElementById('startupPageSel');
  if(sel&&j.page)sel.value=j.page;
 }catch(e){}
}
async function saveStartupPage(){
 const v=document.getElementById('startupPageSel').value;
 try{
  const r=await fetch('/api/startuppage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page:v})});
  const j=await r.json();
  if(j.ok===false)alert('保存失败: '+j.error);
  else if(v==='default')alert('已设置：下次启动打开 default 工作页（会自动等待就绪并重试）');
 }catch(e){alert('保存请求失败: '+e);}
}
async function loadCreds(){
 const r=await fetch('/api/creds');const j=await r.json();
 const c=document.getElementById('credCards');c.innerHTML='';
 const list=j.refs||[];
 if(!list.length){c.innerHTML='<div class="card"><span style="color:#8f9bd0">暂无凭据</span></div>';return;}
 for(const it of list){
  c.innerHTML+=`<div class="card plug-card"><div class="head"><span class="name pname">${it.name}</span> <span class="badge ${it.used?'on':''}" title="${it.used?'该 key 正被 provider 使用':'未被任何 provider 引用'}">${it.label}${it.used?' · 在用':''}</span></div><div class="pmeta" id="cv-${it.name}" style="font-family:Consolas,monospace">${it.masked}</div><div class="pops"><button class="ghost" onclick="credReveal('${it.name}')">显示</button><button onclick="credFill('${it.name}')">填入表单</button><button class="ghost danger" onclick="credDel('${it.name}')">删除</button></div></div>`;
 }
}
async function credReveal(name){
 const el=document.getElementById('cv-'+name);
 const btn=el.nextElementSibling.querySelector('button');
 if(el.getAttribute('data-open')!=='1'){
  const raw=await (await fetch('/api/cred?name='+encodeURIComponent(name))).json();
  el.textContent=raw.key||'(空)';el.setAttribute('data-open','1');btn.textContent='隐藏';
 }else{
  const j=await (await fetch('/api/creds')).json();
  const it=j.refs.find(x=>x.name===name);
  el.textContent=it?it.masked:'';el.setAttribute('data-open','0');btn.textContent='显示';
 }
}
async function credFill(name){
 document.getElementById('credName').value=name;
 const j=await (await fetch('/api/creds')).json();
 const it=j.refs.find(x=>x.name===name);
 if(it){const raw=await (await fetch('/api/cred?'+encodeURIComponent(name))).json();document.getElementById('credKey').value=raw.key||'';}
}
async function credSave(){
 const name=document.getElementById('credName').value.trim();
 const key=document.getElementById('credKey').value.trim();
 if(!name||!key){alert('凭据名和 Key 都要填');return;}
 const r=await fetch('/api/creds/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,key})});
 const j=await r.json();
 if(j&&j.ok===false){alert('失败: '+j.error);return;}
 alert('已保存「'+name+'」（新 key 在重启 dsh 实例后生效）');
 document.getElementById('credKey').value='';loadCreds();
}
async function credDel(name){
 if(!confirm('确定删除凭据「'+name+'」？'))return;
 const r=await fetch('/api/creds/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
 const j=await r.json();
 if(j&&j.ok===false){alert('失败: '+j.error);return;}
 loadCreds();
}
async function loadPlugins(){
 const r=await fetch('/api/plugins');const j=await r.json();
 const c=document.getElementById('plugCards');c.innerHTML='';
 const list=j.installed||[];document.getElementById('plugCount').textContent=list.length;
 for(const it of list){
  c.innerHTML+=`<div class="card plug-card"><div class="head"><span class="name pname">${it.name}</span><span class="switch ${it.enabled?'on':''}" onclick="togglePlugin('${it.name.replace(/'/g,"\\'")}',${!it.enabled})" title="点击启停"></span></div><div class="pmeta">${it.spec}</div><div class="pops"><span class="badge ${it.enabled?'on':''}">${it.enabled?'已启用':'已禁用'}</span></div></div>`;
 }
}
async function togglePlugin(name,en){
 await fetch('/api/plugin/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,enabled:en})});loadPlugins();
}
async function setAllPlugins(en){
 const r=await fetch('/api/plugins');const j=await r.json();
 for(const it of (j.installed||[])){if(it.enabled!==en)await fetch('/api/plugin/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:it.name,enabled:en})});}
 loadPlugins();
}
async function loadMarket(refresh){
 const q=document.getElementById('mktQ').value.trim();
 document.getElementById('mktCount').textContent='加载中…';
 const r=await fetch('/api/market?q='+encodeURIComponent(q)+(refresh?'&refresh=1':''));const j=await r.json();
 document.getElementById('mktCount').textContent=j.count+' 项'+(j.error?'（'+j.error+'）':'');
 const c=document.getElementById('mktCards');c.innerHTML='';
 for(const p of (j.plugins||[])){
  const d=(p.description&&(p.description.zh||p.description.en))||'';
  const inst=p.install||'';
  let spec=(inst.split(' add ')[1]||'').trim();
  if(!spec)spec=p.npm||p.name;
  c.innerHTML+=`<div class="card plug-card"><div class="head"><span class="name pname">${p.name}</span><span class="port">★${p.stars||0}</span></div><div class="pmeta">${p.owner||''} · ${p.added||''} · ⬇${(p.downloads||0)}</div><div class="pdesc">${String(d).slice(0,140)}</div><div class="pops"><button onclick="installMenu('${encodeURIComponent(p.npm||'')}','${encodeURIComponent(p.owner||'')}','${encodeURIComponent(inst)}','${p.name.replace(/'/g,"\\'")}','${encodeURIComponent(spec)}')">安装 ▾</button></div></div>`;
 }
}
async function installMenu(npm,owner,inst,label,fallbackSpec){
 // 先查三通道（stable/beta/alpha），解析不出通道则用原始 spec 直接装
 let chans={};
 try{const r=await fetch('/api/channels?name='+encodeURIComponent(label)+'&npm='+npm+'&owner='+owner+'&install='+inst);const j=await r.json();chans=j.channels||{};}catch(e){}
 const keys=Object.keys(chans);
 if(!keys.length){
  doInstall(decodeURIComponent(fallbackSpec),label);
  return;
 }
 // 自绘选择弹窗
 const mask=document.createElement('div');
 mask.style.cssText='position:fixed;inset:0;background:rgba(5,8,20,.72);z-index:99;display:flex;align-items:center;justify-content:center';
 const box=document.createElement('div');
 box.style.cssText='background:#1a2038;border:1px solid #2a3050;border-radius:14px;padding:20px;min-width:340px;max-width:440px';
 let html=`<div style="font-weight:600;margin-bottom:4px">选择安装通道 · ${label}</div>
  <div style="color:#8f9bd0;font-size:12px;margin-bottom:12px">stable=官方稳定版 · beta=预发布测试版 · alpha=GitHub 最新代码</div>`;
 for(const k of keys){
  const c=chans[k];
  html+=`<button style="display:block;width:100%;text-align:left;background:${c.color};margin:6px 0;padding:10px 14px;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px" data-spec="${c.spec.replace(/"/g,'&quot;')}">${c.label}<span style="opacity:.75;float:right">${k}</span></button>`;
 }
 html+=`<div style="display:flex;gap:8px;margin-top:10px"><button class="ghost" style="flex:1" id="dCancel">取消</button></div>`;
 box.innerHTML=html;
 mask.appendChild(box);
 document.body.appendChild(mask);
 box.addEventListener('click',e=>{
  const b=e.target.closest('button[data-spec]');
  if(b){document.body.removeChild(mask);doInstall(b.getAttribute('data-spec'),label);}
 });
 document.getElementById('dCancel').onclick=()=>document.body.removeChild(mask);
 mask.onclick=e=>{if(e.target===mask)document.body.removeChild(mask);};
}
async function doInstall(spec,label){
 if(!confirm('确认安装插件「'+label+'」？\n'+spec))return;
 const r=await fetch('/api/plugin/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({spec})});
 const j=await r.json();if(j&&j.ok===false&&j.error){alert('失败: '+j.error);return;}
 setTimeout(loadTasks,500);
}
async function loadAuto(){
 try{
  const j=await (await fetch('/api/autostart')).json();
  const sw=document.getElementById('autoSw');sw.className='switch'+(j.enabled?' on':'');
  document.getElementById('autoState').textContent=j.enabled?'已开启':'已关闭';
  document.getElementById('autoState').className='badge'+(j.enabled?' on':'');
 }catch(e){document.getElementById('autoState').textContent='读取失败: '+e;}
}
async function toggleAuto(){
 try{
  const now=(await (await fetch('/api/autostart')).json()).enabled;
  await fetch('/api/autostart?enable='+(now?'0':'1'),{method:'POST'});
  loadAuto();
 }catch(e){alert('操作失败: '+e);}
}
async function runDoctor(){
 const box=document.getElementById('doctorBox');box.textContent='诊断中…';
 try{
  const j=await (await fetch('/api/doctor')).json();
  box.textContent=j.text||'（无结果）';
 }catch(e){box.textContent='诊断失败: '+e;}
}
async function checkDsh(){return checkUpd('dsh');}
async function checkMkt(){return checkUpd('mkt');}
async function checkUpd(kind){
 const box=document.getElementById(kind+'UpdBox');
 const btn=document.getElementById(kind+'UpdBtn');
 const ch=document.getElementById('dshCh')?document.getElementById('dshCh').value:'stable';
 btn.disabled=true;btn.textContent='检查中…';
 try{
  const j=await (await fetch('/api/upd-chk?s='+kind+'&ch='+ch)).json();
  if(j.busy){box.textContent='已有更新任务进行中，请稍候…';btn.disabled=false;btn.textContent='重新检查';return;}
  if(j.done){box.textContent=j.msg;btn.textContent='重新检查';btn.onclick=()=>checkUpd(kind);btn.disabled=false;return;}
  let msg=j.msg;
  if(j.channels){const c=j.channels;msg+='　[稳定] '+(c.stable||'—')+'　[测试] '+(c.beta&&c.beta!==c.stable?c.beta:'同稳定')+'　[内测] '+(c.alpha||'—');}
  box.innerHTML=msg;
  if(j.updatable){
   btn.textContent='立即更新 '+j.target;btn.onclick=()=>doUpd(kind,ch,j.target);
  }else{
   btn.textContent='已是最新';btn.onclick=()=>checkUpd(kind);
  }
  btn.disabled=false;
 }catch(e){box.textContent='检查失败: '+e;btn.textContent='重试';btn.disabled=false;}
}
function setMsg(kind,txt,cls){
 const box=document.getElementById(kind+'UpdBox');
 box.innerHTML=`<span class="badge ${cls}">${txt}</span>`;
}
async function doUpd(kind,ch,target){
 const btn=document.getElementById(kind+'UpdBtn');
 btn.disabled=true;btn.textContent='更新中…';
 // 更新前探测插件兼容性（仅 dsh 本体）
 if(kind==='dsh'){
  try{
   const pj=await (await fetch('/api/plugcompat?ch='+ch)).json();
   const bad=(pj.rows||[]).filter(r=>!r.ok);
   if(bad.length){
    const lines=bad.slice(0,8).map(r=>'  - '+r.name+' 需要 '+r.req).join('\n');
    const more=bad.length>8?('\n  …还有 '+(bad.length-8)+' 个'):'';
    if(!confirm('目标 '+ch+' 通道 dsh-tools 将解析为 '+pj.tools_version+'\n探测到 '+bad.length+' 个插件可能不兼容：\n'+lines+more+'\n\n仍要继续更新吗？（可先到「插件兼容性」卡片一键修复）')){
     setMsg(kind,'已取消更新','err');btn.textContent='重新检查';btn.onclick=()=>checkUpd(kind);btn.disabled=false;return;
    }
   }
  }catch(e){}
 }
 setMsg(kind,target?'正在更新到 '+target+' …':'正在更新…','on');
 try{
  const r=await fetch('/api/upd-do?s='+kind+'&ch='+ch,{method:'POST'});
  const j=await r.json();
  if(j&&j.ok===false){setMsg(kind,'启动失败: '+j.error,'err');btn.textContent='重试';btn.disabled=false;return;}
 }catch(e){setMsg(kind,'启动失败: '+e,'err');btn.textContent='重试';btn.disabled=false;return;}
 // 轮询直到完成
 const t0=Date.now();
 while(Date.now()-t0<180000){
  await new Promise(r=>setTimeout(r,2500));
  const j=await (await fetch('/api/upd-chk?s='+kind+'&ch='+ch)).json();
  if(j.done===true){
   if(j.ok){setMsg(kind,'更新完成，重启实例后生效','ok');}
   else setMsg(kind,'更新失败，见 DSH.log','err');
   btn.textContent='重新检查';btn.onclick=()=>checkUpd(kind);btn.disabled=false;return;
  }
 }
 setMsg(kind,'超时未完成，请查看 DSH.log','err');btn.textContent='重试';btn.disabled=false;
}
async function plugCompat(){
 const box=document.getElementById('plugBox');
 const ch=document.getElementById('plugCh').value;
 box.textContent='检测中…';
 try{
  const j=await (await fetch('/api/plugcompat?ch='+ch)).json();
  if(j.error){box.textContent=j.error;return;}
  let h='<div style="white-space:pre-wrap">目标 dsh: '+j.target_dsh+'　dsh-tools: '+j.tools_version+(j.tools_required?('（声明 '+j.tools_required+'）'):'')+'</div>';
  const rows=j.rows||[];
  for(const r of rows){
   const cls=r.ok?'on':'err';
   h+=`<div class="pops"><span class="badge ${cls}">${r.ok?'兼容':'冲突'}</span><span class="pmeta">${r.name}</span><span style="color:#8f9bd0;font-size:12px;margin-left:auto">${r.req?('需要 '+r.req):r.note}</span></div>`;
  }
  if(!rows.length) h+='<div style="color:#8f9bd0">未扫描到已安装插件</div>';
  box.innerHTML=h;
 }catch(e){box.textContent='检测失败: '+e;}
}
async function plugFix(){
 const box=document.getElementById('plugBox');
 const btn=document.getElementById('plugFixBtn');
 btn.disabled=true;btn.textContent='修复中…';
 try{
  const r=await fetch('/api/plugfix',{method:'POST'});
  const j=await r.json();
  box.innerHTML=(j.logs||[]).map(s=>'· '+s).join('<br>');
  if(j.plugins){
   const bad=(j.plugins||[]).filter(p=>!p.ok);
   box.innerHTML+='<br><b>'+(bad.length?'仍有 '+bad.length+' 个插件不兼容（需降级 dsh 或禁用对应插件）':'所有插件兼容')+'</b>';
  }
  if(!j.ok) box.innerHTML+='<br><span class="badge err">存在不兼容问题，需手动处理</span>';
 }catch(e){box.textContent='修复失败: '+e;}
 btn.disabled=false;btn.textContent='一键修复';
}
async function loadTasks(){
 const r=await fetch('/api/tasks');const j=await r.json();
 const c=document.getElementById('taskList');c.innerHTML='';
 document.getElementById('taskCount').textContent=j.tasks.length;
 for(const t of (j.tasks||[]).slice().reverse()){
  const st=t.status==='running'?'安装中…':'结束';
  c.innerHTML+=`<div class="card plug-card"><div class="pops"><span class="badge ${t.status==='running'?'on':'err'}">${st}</span><span class="pmeta">${t.spec} · ${t.id}</span></div><div class="task-box">${(t.tail||[]).join('\n')}</div></div>`;
 }
}
function mobileConn(){mobileOn();}
async function mobileOn(){
 if(!confirm('开启手机连接？\n\n会把 default 实例的 dsh 网页开放到局域网（须与本机同一 WiFi，链接含访问令牌，请勿外传）。'))return;
 const btn=document.getElementById('btnMobile');if(btn)btn.disabled=true;
 try{
  const r=await fetch('/api/mobile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:true})});
  const j=await r.json();
  if(!j.ok){alert('开启失败: '+(j.error||'未知错误'));return;}
  document.getElementById('mobileUrl').value=j.url||'';
  document.getElementById('mobileBox').style.display='flex';
 }catch(e){alert('手机连接请求失败: '+e);}
 finally{if(btn)btn.disabled=false;}
}
async function mobileOff(){
 const btn=document.getElementById('btnMobileOff');if(btn)btn.disabled=true;
 try{
  const r=await fetch('/api/mobile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:false})});
  const j=await r.json();
  document.getElementById('mobileBox').style.display='none';
  if(!j.ok)alert('关闭失败: '+(j.error||''));
  else refresh();
 }catch(e){alert('关闭请求失败: '+e);}
 finally{if(btn)btn.disabled=false;}
}
function closeMobile(){document.getElementById('mobileBox').style.display='none';}
function copyMobile(){
 const v=document.getElementById('mobileUrl').value;
 if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(v).then(()=>alert('已复制链接'));
 else{const t=document.createElement('textarea');t.value=v;document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);alert('已复制链接');}
}
let cronSel=null;
async function fillCronInsts(){
 try{
  const r=await fetch('/api/status');const list=await r.json();
  const sel=document.getElementById('cronInst');if(!sel)return;
  const cur=sel.value;sel.innerHTML='';
  for(const it of list)sel.innerHTML+=`<option value="${it.name}"${it.name===cur?' selected':''}>${it.name} (端口 ${it.port})</option>`;
  if(cur&&!list.some(x=>x.name===cur))sel.value='default';
 }catch(e){}
}
async function loadCron(){
 try{
  const r=await fetch('/api/cron');const j=await r.json();
  document.getElementById('cronCount').textContent=j.tasks.length;
  const c=document.getElementById('cronCards');
  if(!j.tasks.length){c.innerHTML='<div class="plug-card" style="color:#8f9bd0">还没有定时任务，点「＋ 新增任务」创建一个。任务到点后会自动启动目标实例、在工作目录向 dsh 发送需求，完成弹桌面提醒。</div>';return;}
  c.innerHTML='';
  for(const t of j.tasks){
   const running=t.state==='running';
   const statCls=running?'on':(t.state==='error'?'err':'');
   const stText={idle:'待命',running:'执行中',done:'已完成',error:'失败'}[t.state]||t.state;
   const onoff=t.enabled?`<button onclick="cronToggle('${t.id}',false)">停用</button>`:`<button onclick="cronToggle('${t.id}',true)">启用</button>`;
   const desc=(t.desc||'').replace(/</g,'&lt;');
   c.innerHTML+=`<div class="card" style="${cronSel===t.id?'border-color:#4d6bfe':''}">
    <div class="head"><span class="dot ${statCls} ${t.enabled?'':'off'}"></span><span class="name" title="${desc}">${t.name}</span><span class="badge ${statCls}">${stText}</span><span class="port">${t.schedule_text}</span></div>
    <div class="ws">实例: ${t.inst_text} · 工作目录: ${t.workspace||'（实例默认）'}<br>需求: ${desc.slice(0,60)}${desc.length>60?'…':''}</div>
    <div class="pops"><span class="pmeta">下次: ${t.next_text} · 上次: ${t.last_text} · 已执行 ${t.runs||0} 次</span></div>
    ${t.last_error?`<div class="pmeta" style="color:#e77">最近错误: ${t.last_error.replace(/</g,'&lt;')}</div>`:''}
    <div class="ops" style="margin-top:8px">
     ${running?`<button class="ghost" disabled>执行中…</button>`:`<button onclick="cronRun('${t.id}')">立即执行</button>`}
     <button class="ghost" onclick="cronView('${t.id}')">输出</button>
     ${onoff}
     <button class="ghost danger" onclick="cronDel('${t.id}')">删除</button>
    </div></div>`;
  }
 }catch(e){document.getElementById('cronCards').textContent='加载失败: '+e;}
}
async function cronView(id){
 cronSel=id;loadCron();
 try{
  const r=await fetch('/api/cron');const j=await r.json();
  const t=j.tasks.find(x=>x.id===id);
  const box=document.getElementById('cronLogBox');
  if(!t){box.textContent='任务不存在';return;}
  box.textContent='【'+t.name+'】最近输出：\n'+(t.tail&&t.tail.length?t.tail.join('\n'):'（暂无输出）');
 }catch(e){document.getElementById('cronLogBox').textContent='读取失败: '+e;}
}
function cronNew(){
 document.getElementById('cronBoxTitle').textContent='新增定时任务';
 document.getElementById('cronName').value='';
 document.getElementById('cronDesc').value='';
 document.getElementById('cronWs').value='';
 document.getElementById('cronSched').selectedIndex=0;
 cronSchedUI();fillCronInsts();
 document.getElementById('cronBox').style.display='flex';
 document.getElementById('cronName').focus();
}
function cronClose(){document.getElementById('cronBox').style.display='none';}
function cronSchedUI(){
 const s=document.getElementById('cronSched').value;
 const line=document.getElementById('cronParamLine');
 line.style.display='block';
 let h='';
 if(s==='once')h=`<div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">执行时间（本地时间）*</div><input id="cronOnce" type="datetime-local" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid #2a3050;background:#0d1128;color:#e8eaf6;outline:none">`;
 if(s==='interval')h=`<div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">间隔（分钟，1-10080）*</div><input id="cronIntv" type="number" min="1" max="10080" value="60" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid #2a3050;background:#0d1128;color:#e8eaf6;outline:none">`;
 if(s==='daily')h=`<div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">每天执行时刻 *</div><input id="cronDayT" type="time" value="09:00" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid #2a3050;background:#0d1128;color:#e8eaf6;outline:none">`;
 if(s==='weekly'){
  let w='';
  for(let i=1;i<=7;i++)w+=`<option value="${i}"${i===1?' selected':''}>周${'一二三四五六日'[i-1]}</option>`;
  h=`<div style="color:#8f9bd0;font-size:12px;margin-bottom:4px">每个星期 *</div><div style="display:flex;gap:10px"><select id="cronWeekD" style="flex:1">${w}</select><input id="cronWeekT" type="time" value="09:00" style="width:130px"></div>`;
 }
 line.innerHTML=h;
}
function cronParam(){
 const s=document.getElementById('cronSched').value,p={};
 if(s==='once'){const v=document.getElementById('cronOnce').value;if(v)p.run_at=new Date(v).getTime()/1000;}
 if(s==='interval')p.interval_min=parseInt(document.getElementById('cronIntv').value)||60;
 if(s==='daily')p.time=document.getElementById('cronDayT').value||'09:00';
 if(s==='weekly'){p.weekday=parseInt(document.getElementById('cronWeekD').value)||1;p.time=document.getElementById('cronWeekT').value||'09:00';}
 return p;
}
function cronCollect(){
 return {
  name:document.getElementById('cronName').value,
  desc:document.getElementById('cronDesc').value,
  schedule:document.getElementById('cronSched').value,
  param:cronParam(),
  inst:document.getElementById('cronInst').value,
  workspace:document.getElementById('cronWs').value
 };
}
async function cronSave(runNow){
 const d=cronCollect();
 if(!d.name){alert('请填写任务名称');return;}
 if(!d.desc){alert('请填写任务需求');return;}
 if(d.schedule==='once'&&!d.param.run_at){alert('请选择执行时间');return;}
 try{
  const r=await fetch('/api/cron/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  const j=await r.json();
  if(!j.ok){alert('保存失败: '+j.error);return;}
  cronClose();loadCron();
  if(runNow&&j.id){await fetch('/api/cron/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:j.id})});cronSel=j.id;setTimeout(loadCron,1500);}
 }catch(e){alert('保存请求失败: '+e);}
}
async function cronRun(id){
 const r=await fetch('/api/cron/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
 const j=await r.json();
 if(!j.ok){alert('启动失败: '+j.error);return;}
 cronSel=id;loadCron();
 setTimeout(cronView,800,id);
}
async function cronToggle(id,on){
 const r=await fetch('/api/cron/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,enabled:on})});
 const j=await r.json();
 if(!j.ok){alert('操作失败: '+j.error);return;}
 loadCron();
}
async function cronDel(id){
 if(!confirm('确认删除该定时任务？'))return;
 const r=await fetch('/api/cron/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
 const j=await r.json();
 if(!j.ok){alert('删除失败: '+j.error);return;}
 if(cronSel===id)cronSel=null;
 loadCron();
}
refresh();setInterval(refresh,3000);loadUsage();setInterval(loadUsage,60000);loadPlugins();
</script>
<div id="mobileBox" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:50;align-items:center;justify-content:center">
<div style="background:#1a2038;border:1px solid #3a4166;border-radius:14px;padding:22px;width:520px;max-width:92vw">
<div class="head"><b>手机连接 · 同 WiFi 可访问</b><span style="flex:1"></span><button class="ghost" onclick="closeMobile()">关闭</button></div>
<p style="color:#aab2d8;font-size:13px;margin:12px 0">手机浏览器打开下面的链接即可操控本机 dsh（请保持与本机同一 WiFi）：</p>
<div style="display:flex;gap:8px;margin:12px 0">
<input id="mobileUrl" readonly style="flex:1;padding:8px 10px;border-radius:8px;border:1px solid #2a3050;background:#0d1128;color:#8fd7a5;font-family:Consolas,monospace;font-size:12px;outline:none" onfocus="this.select()">
<button onclick="copyMobile()">复制</button>
</div>
<p style="color:#8f9bd0;font-size:12px">链接包含访问令牌，仅限可信家人/同事使用；用完请点下方按钮关闭对局域网的开放。</p>
<button id="btnMobileOff" class="danger" onclick="mobileOff()">关闭手机连接</button>
</div>
</div>
</body>
</html>"""

def _inst_json():
    now = time.time()
    out = []
    for i in INSTANCES:
        st = i.status()
        out.append({"name": i.name, "port": i.port, "workspace": i.workspace,
                    "status": st, "external": i.external,
                    "env": i.env, "dshhome": i.dshhome or "",
                    "uptime": int(now - i.start_ts) if st == "running" and i.start_ts else 0})
    return out

def _find_inst(name):
    for i in INSTANCES:
        if i.name == name:
            return i
    return None

class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        p = self.path.split("?")[0]
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        arg = lambda k: q.get(k, [""])[0]
        if p == "/api/status":
            self._send(200, json.dumps(_inst_json()), "application/json; charset=utf-8")
        elif p == "/api/usage":
            self._send(200, json.dumps({"balance": deepseek_balance(),
                                        "sessions": session_stats()},
                                       ensure_ascii=False), "application/json; charset=utf-8")
        elif p == "/api/plugins":
            self._send(200, json.dumps({"installed": installed_plugins()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/market":
            refresh = arg("refresh") == "1"
            data, err = get_market(refresh=refresh)
            kw = arg("q").strip().lower()
            if kw:
                data = [x for x in data if kw in (x.get("name") or "").lower()
                        or kw in (x.get("owner") or "").lower()
                        or kw in _desc_text(x.get("description")).lower()]
            body = {"count": len(data), "plugins": data[:200]}
            if err:
                body["error"] = err
            self._send(200, json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")
        elif p == "/api/creds":
            self._send(200, json.dumps({"refs": list_credentials()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/cred":
            name = arg("name")
            key = read_credentials_keys().get(name, "")
            if key:
                self._send(200, json.dumps({"key": key}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, json.dumps({"error": "凭据不存在"}), "application/json; charset=utf-8")
        elif p == "/api/tasks":
            tasks = list(_tasks.values())
            for t in tasks:
                t["tail"] = t["log"][-12:]
            self._send(200, json.dumps({"tasks": tasks}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/channels":
            chans = plugin_channels(arg("name"), arg("npm"), arg("owner"), arg("install"))
            self._send(200, json.dumps({"channels": chans}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/doctor":
            self._send(200, json.dumps({"checks": run_doctor(),
                                        "text": run_doctor_text()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/autostart":
            self._send(200, json.dumps({"enabled": autostart_enabled()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/upd-chk":
            kind = arg("s") in ("mkt",) and "mkt" or "dsh"
            self._send(200, json.dumps(upd_check(kind, arg("ch") or "stable"), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/plugcompat":
            self._send(200, json.dumps(plugin_compat_full(arg("ch") or "current"), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/cron":
            self._send(200, json.dumps({"tasks": cron_serialize()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/startuppage":
            self._send(200, json.dumps({"page": get_startup_page()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(200, ADMIN_HTML, "text/html; charset=utf-8")

    def do_POST(self):
        p = self.path.split("?")[0]
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        arg = lambda k: q.get(k, [""])[0]
        name = arg("name")
        inst = _find_inst(name)
        if p == "/api/start" and inst:
            inst.start()
        elif p == "/api/stop" and inst:
            inst.stop()
        elif p == "/api/open" and inst:
            inst.open_web()
        elif p == "/api/allstart":
            for i in INSTANCES:
                i.start()
        elif p == "/api/allstop":
            for i in INSTANCES:
                i.stop()
        elif p == "/api/add":
            err = add_instance(arg("name"), arg("port"), arg("workspace"), arg("dshhome"))
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/del":
            err = del_instance(name)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/plugin/enable":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            err = set_plugin_enabled(data.get("name", ""), bool(data.get("enabled")))
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/plugin/install":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            spec = (data.get("spec") or "").strip()
            if not spec:
                self._send(400, json.dumps({"ok": False, "error": "缺少插件标识"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            task_id = plugin_install(spec)
            self._send(200, json.dumps({"ok": True, "task": task_id}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        elif p == "/api/creds/set":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            err = set_credential(data.get("name", ""), data.get("key", ""))
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/creds/del":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            err = del_credential(data.get("name", ""))
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/setenv":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            inst = _find_inst(data.get("name", ""))
            if not inst:
                self._send(400, json.dumps({"ok": False, "error": "实例不存在: " + data.get("name", "")}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            err = set_instance_config(inst, data)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/autostart":
            enable = arg("enable") == "1"
            err = set_autostart(enable)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/embed":
            err = open_embedded(name)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/tui":
            err = open_tui(name)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/setport":
            err = set_port(name, arg("port"))
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/upd-do":
            kind = arg("s") in ("mkt",) and "mkt" or "dsh"
            err = upd_start(kind, arg("ch") or "stable")
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/plugfix":
            self._send(200, json.dumps(plugin_quick_fix(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/mobile":
            try:
                data = self._json_body()
            except Exception:
                data = {}
            try:
                res = set_mobile(bool(data.get("on")))
            except Exception as e:
                res = {"ok": False, "on": bool(data.get("on")), "error": "手机连接异常: %s" % e}
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            return
        elif p in ("/api/cron/add", "/api/cron/del", "/api/cron/toggle", "/api/cron/run"):
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            cid = data.get("id", "")
            if p == "/api/cron/add":
                err = cron_add(data)
            elif p == "/api/cron/del":
                err = cron_del(cid)
            elif p == "/api/cron/toggle":
                err = cron_toggle(cid, bool(data.get("enabled")))
            else:
                err = run_cron_job_async(cid)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            elif p == "/api/cron/add":
                self._send(200, json.dumps({"ok": True, "id": _CRON_LAST_NEW_ID[0]}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
        elif p == "/api/startuppage":
            try:
                data = self._json_body()
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "请求体必须是 JSON"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            set_startup_page(data.get("page") or "admin")
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

def _serve_admin(srv):
    try:
        srv.serve_forever()
    except Exception:
        import traceback
        log("管理服务异常: " + traceback.format_exc())

# ---------- 开机自启（注册表 HKCU Run） ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DSHDock"

def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False

def set_autostart(enable):
    try:
        import winreg
    except ImportError:
        return "系统不支持注册表自启"
    try:
        exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        if enable:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, '"' + exe + '"')
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass
        winreg.CloseKey(key)
        log(f"[自启] {'已启用' if enable else '已禁用'}: {exe}")
        return None
    except Exception as e:
        return "设置自启失败: " + str(e)

# ---------- 环境诊断（dsh doctor） ----------
def run_doctor():
    """系统体检：环境、dsh、profile、端口、日志、凭据，返回结构化诊断列表"""
    checks = []
    def add(k, v, ok=True, hint=""):
        checks.append({"k": k, "v": str(v), "ok": bool(ok), "hint": hint})

    # Node.js
    node = shutil.which("node")
    if node:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            add("Node.js", (r.stdout or r.stderr).strip() or node, True)
        except Exception as e:
            add("Node.js", node, False, str(e))
    else:
        add("Node.js", "未找到", False, "需安装 Node.js ≥ 22（https://nodejs.org）")

    # npm / pnpm
    for name in ("npm", "pnpm"):
        exe = shutil.which(name)
        if exe:
            try:
                # Windows 下 npm/pnpm 是 .cmd 批处理，需经 cmd /c 执行
                cmd_list = ["cmd", "/c", name, "--version"]
                r = subprocess.run(cmd_list, capture_output=True, text=True, timeout=8,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                add(name, (r.stdout or r.stderr).strip() or exe, True)
            except Exception as e:
                add(name, exe, False, str(e))
        else:
            add(name, "未找到", False, "这是插件市场安装所必需的")

    # dsh 全局安装
    if DSH:
        node_p, bin_js = DSH
        add("dsh (bin.js)", bin_js)
        v = get_local_version()
        add("dsh 版本", v or "读取失败", bool(v))
    else:
        add("dsh", "未找到", False, "npm install -g @deepseek-ai/dsh")

    # 与最新版对比
    if ver_latest:
        add("dsh 最新版", ver_latest, ver_latest == ver_local,
            "" if ver_latest == ver_local else f"本地 {ver_local}，可在托盘菜单一键升级")

    # web profile
    if os.path.isfile(PKG_FILE):
        try:
            pkg = json.load(open(PKG_FILE, encoding="utf-8"))
            deps = pkg.get("dependencies") or {}
            bundles = (pkg.get("dsh", {}).get("profile", {}) or {}).get("bundles") or []
            add("web profile", f"已装 {len(deps)} 个插件 / 启用 {len(bundles)} 个 bundle")
            broken = [b for b in bundles if b not in deps and b != "@deepseek-ai/dsh-base" and b != "@deepseek-ai/dsh-web-app"]
            if broken:
                add("bundle 一致性", "发现 %d 个已声明但未声明的 bundle: %s" % (len(broken), ", ".join(broken)), False,
                    "删除对应 dependencies 或 bundle 声明")
        except Exception as e:
            add("web profile", "解析失败", False, str(e))
    else:
        add("web profile", PKG_FILE, False, "profile 缺失，运行 dsh web 一次会自动初始化")

    # DSH_HOME
    add("DSH_HOME", DSH_HOME_DIR, os.path.isdir(DSH_HOME_DIR))
    cred = os.path.join(DSH_HOME_DIR, ".credentials.yaml")
    add("凭据文件", cred, os.path.isfile(cred), "" if os.path.isfile(cred) else "尚未配置任何 API Key")

    # 端口（管理界面本身）
    for i in INSTANCES:
        add(f"端口 {i.port} ({i.name})", "占用中" if is_open(i.port) else "空闲", True)

    # 各实例 DSH_HOME
    for i in INSTANCES:
        if i.dshhome:
            add(f"实例 {i.name} DSH_HOME", i.dshhome, os.path.isdir(i.dshhome),
                "" if os.path.isdir(i.dshhome) else "目录不存在，下次启动会自动创建")

    # 日志
    add("日志文件", LOG_FILE, True, f"{os.path.getsize(LOG_FILE)} 字节" if os.path.isfile(LOG_FILE) else "尚无日志")
    return checks

def run_doctor_text():
    """把诊断列表渲染成纯文本报告（含脱敏），方便直接看 / 复制"""
    lines = ["DSH Doctor 诊断报告", "=" * 40]
    for c in run_doctor():
        mark = "OK " if c["ok"] else "FAIL"
        lines.append(f"[{mark}] {c['k']}: {c['v']}")
        if c["hint"]:
            lines.append(f"       -> {c['hint']}")
    return "\n".join(lines)

# ---------- 内嵌浏览器（pywebview 独立子进程） ----------
def open_embedded(name):
    """用 pywebview 在应用内窗口打开指定实例（或第一个实例）的 dsh 页面。

    子进程方式：GUI 消息循环在子进程主线程跑，不干扰托盘/HTTP 线程；窗口关了进程自动退出。
    """
    inst = _find_inst(name) or (INSTANCES[0] if INSTANCES else None)
    if not inst:
        return "没有可打开的实例"
    url = f"http://127.0.0.1:{inst.port}"
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, os.path.abspath(__file__)]
    env = dict(os.environ)
    env["DSH_EMBED_URL"] = url
    try:
        p = subprocess.Popen(cmd, env=env, cwd=os.path.dirname(os.path.abspath(__file__)),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return "内嵌启动失败: " + str(e)
    time.sleep(2.5)
    if p.poll() is not None and p.returncode != 0:
        return "内嵌窗口进程异常退出(rc=%s)" % p.returncode
    log(f"[内嵌窗口] 打开 {inst.name} ({inst.port}) -> {url}")
    return None

# ---------- TUI 终端（Windows Terminal 优先，conhost 兜底） ----------
TUI_PROFILE = "dsh-tui"  # dsh-tui 插件要求 profile 名必须叫这个

def open_tui(name):
    """在新终端窗口里跑 `dsh --profile dsh-tui`（工作目录 = 该实例工作区）。

    优先用 Windows Terminal（对 TUI 渲染最友好），没有则回退 conhost 新窗口。
    """
    inst = _find_inst(name) or (INSTANCES[0] if INSTANCES else None)
    if not inst:
        return "没有可打开的实例"
    ws = inst.workspace or "."
    node, bin_js = DSH
    if not bin_js:
        return "未找到 dsh（npm 全局安装的 @deepseek-ai/dsh）"
    dsh_cmd = subprocess.list2cmdline([node, bin_js, "--profile", TUI_PROFILE])
    wt = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                      "Microsoft", "WindowsApps", "wt.exe")
    if os.path.isfile(wt):
        cmd = f'wt -d "{ws}" {dsh_cmd}'
        creation = 0
    else:
        cmd = f'cmd /c start "DSH Dock TUI" cmd /k "cd /d {ws} && {dsh_cmd}"'
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(cmd, cwd=ws, shell=True, creationflags=creation)
    except Exception as e:
        return "TUI 启动失败: " + str(e)
    log(f"[TUI] 打开 {inst.name} 工作区 {ws}")
    return None

def start_admin_server():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", ADMIN_PORT), partial(AdminHandler))
        srv.daemon_threads = True
        threading.Thread(target=_serve_admin, args=(srv,), daemon=True).start()
        log(f"管理界面: http://127.0.0.1:{ADMIN_PORT}")
    except OSError as e:
        log(f"管理界面启动失败: {e}")

# ---------- 版本工具（semver / 更新通道 / 插件兼容） ----------

def _ver_key(v):
    """带 prerelease 的完整比较键；无 prerelease 视为比所有 prerelease 大"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?$", str(v or "").strip())
    if not m:
        return None
    pre = (m.group(4) or "").split(".") if m.group(4) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)

def _cmp_ver_key(a, b):
    """比较两个 _ver_key；None 视为无 prerelease"""
    for i in range(3):
        if a[i] != b[i]:
            return -1 if a[i] < b[i] else 1
    ap, bp = a[3] or (), b[3] or ()
    if not ap and not bp:
        return 0
    if not ap:
        return 1  # 稳定 > 预发布
    if not bp:
        return -1
    for x, y in zip(ap, bp):
        xi, yi = x.isdigit(), y.isdigit()
        if xi and yi:
            nx, ny = int(x), int(y)
            if nx != ny:
                return -1 if nx < ny else 1
        elif x != y:
            return -1 if xi else 1  # 数字段 < 字母段
    return -1 if len(ap) < len(bp) else (1 if len(ap) > len(bp) else 0)

def _cmp_ver(a, b):
    ka, kb = _ver_key(a), _ver_key(b)
    if ka is None or kb is None:
        return 0
    return _cmp_ver_key(ka, kb)

def _in_range(ver, lo, hi):
    """ver >= lo 且 ver < hi；lo/hi 为 None 表示无界"""
    k = _ver_key(ver)
    if k is None:
        return False
    if lo is not None and _cmp_ver_key(k, lo) < 0:
        return False
    if hi is not None and _cmp_ver_key(k, hi) >= 0:
        return False
    return True

def _bounds_caret(tgt):
    """^x.y.z[-pre] 的 (lo, hi)"""
    k = _ver_key(tgt)
    if k is None:
        return None, None
    lo = k
    if lo[0] > 0:
        hi = (lo[0] + 1, 0, 0, None)
    elif lo[1] > 0:
        hi = (0, lo[1] + 1, 0, None)
    else:
        hi = (0, 0, lo[2] + 1, None)
    return lo, hi

def _bounds_tilde(tgt):
    """~x.y.z 的 (lo, hi)"""
    k = _ver_key(tgt)
    if k is None:
        return None, None
    return k, (k[0], k[1] + 1, 0, None)

def _eval_range_cond(ver, cond):
    """评估单个范围条件（^ / ~ / >= / <= / > / < / = / 裸版本 / 分支。返回 True/False"""
    cond = (cond or "").strip()
    if not cond or cond in ("*", "latest", "x"):
        return True
    if cond.startswith("^"):
        lo, hi = _bounds_caret(cond[1:].strip())
        return _in_range(ver, lo, hi)
    if cond.startswith("~"):
        lo, hi = _bounds_tilde(cond[1:].strip())
        return _in_range(ver, lo, hi)
    m = re.match(r"^(>=|<=|>|<|=|v?)(.*)$", cond)
    op, tgt = m.group(1) or "=", m.group(2).strip()
    tk = _ver_key(tgt)
    if tk is None:
        # 分支形式 1.2 / 1 / 1.2.x / 1.x → 宽松范围匹配
        body = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(?:x|X|\*))?$", tgt)
        if not body:
            return False
        lo = _ver_key("%s.%s.0" % (body.group(1), body.group(2) or "0"))
        if body.group(2):
            hi = (int(body.group(1)), int(body.group(2)) + 1, 0, None)
        else:
            hi = (int(body.group(1)) + 1, 0, 0, None)
        return _in_range(ver, lo, hi)
    c = _cmp_ver(ver, tgt)
    return {"=": c == 0, ">": c > 0, ">=": c >= 0, "<": c < 0, "<=": c <= 0}.get(op, c == 0)

def ver_satisfies(version, spec):
    """近似判断 version 是否满足 semver 范围（支持 || 或、空格/逗号与、^ ~ >= 等）"""
    if not spec:
        return True
    for or_part in str(spec).split("||"):
        ok = True
        for cond in re.split(r"[,\s]+", or_part.strip()):
            cond = cond.strip()
            if cond and not _eval_range_cond(version, cond):
                ok = False
                break
        if ok and or_part.strip():
            return True
    return False

def get_dsh_channels():
    """查询 npm dist-tags，返回 {stable, beta, alpha}（值可为 None）"""
    try:
        req = urllib.request.Request("https://registry.npmjs.org/@deepseek-ai/dsh",
                                     headers={"User-Agent": "dsh-launcher"})
        with urllib.request.urlopen(req, timeout=10) as r:
            tags = (json.load(r).get("dist-tags") or {})
        return {"stable": tags.get("latest"), "beta": tags.get("next"),
                "alpha": tags.get("alpha")}
    except Exception:
        return {"stable": None, "beta": None, "alpha": None}

def _registry_meta(pkg, ver=None):
    """拉取某 npm 包的元数据；ver 省略拉最新"""
    try:
        url = "https://registry.npmjs.org/%s" % urllib.parse.quote(pkg)
        if ver is not None:
            url += "/" + urllib.parse.quote(ver)
        req = urllib.request.Request(url, headers={"User-Agent": "dsh-launcher"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None

def get_target_tools_range(dsh_ver):
    """近似求某 dsh 版本最终携带的 @deepseek-ai/dsh-tools 依赖范围。
    dsh 本体不直接声明 dsh-tools，由 dsh-base 等基础包携带，按依赖链向下解析几个候选。"""
    meta = _registry_meta("@deepseek-ai/dsh", dsh_ver)
    if not meta:
        return None
    candidates = []
    for name, spec in (meta.get("dependencies") or {}).items():
        if name == "@deepseek-ai/dsh-tools":
            return spec
        if name.startswith("@deepseek-ai/dsh-") or "@deepseek-ai" in name:
            candidates.append(name)
    # 优先稳定同名的基础包，最多探测 5 个候选
    order = [c for c in candidates if c.endswith("-base")] + \
            [c for c in candidates if c.endswith("-sdk")] + candidates
    seen = set()
    for cand in order:
        if cand in seen:
            continue
        seen.add(cand)
        if len(seen) > 5:
            break
        cm = _registry_meta(cand)
        if not cm:
            continue
        for n, spec in (cm.get("dependencies") or {}).items():
            if n == "@deepseek-ai/dsh-tools":
                return spec
        for n, spec in (cm.get("peerDependencies") or {}).items():
            if n == "@deepseek-ai/dsh-tools":
                return spec
    return None

def dsh_pkg_dir():
    """全局 dsh 包目录（node_modules/@deepseek-ai/dsh），其下含内嵌 dsh-tools 副本"""
    if not DSH:
        return None
    return os.path.dirname(os.path.dirname(DSH[1]))

def get_global_tools_version():
    """全局 dsh 内嵌 dsh-tools 的实际版本"""
    base = dsh_pkg_dir()
    if not base:
        return None
    try:
        p = os.path.join(base, "node_modules", "@deepseek-ai", "dsh-tools", "package.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None

def installed_plugin_reqs():
    """扫描 web profile 显式安装的用户插件，取其声明的 dsh-tools 依赖范围"""
    try:
        with open(PKG_FILE, encoding="utf-8") as f:
            deps = json.load(f).get("dependencies") or {}
    except Exception:
        return []
    out = []
    for name in sorted(deps):
        pkg_json = os.path.join(PROFILE_DIR, "node_modules", *name.split("/"), "package.json")
        if os.path.isfile(pkg_json) and not name.endswith(".dup"):
            _plugin_req_append(out, name, pkg_json)
    return out

def _plugin_req_append(out, name, pkg_json):
    try:
        with open(pkg_json, encoding="utf-8") as f:
            pkg = json.load(f)
    except Exception:
        return
    peer = pkg.get("peerDependencies") or {}
    d = pkg.get("dependencies") or {}
    req = peer.get("@deepseek-ai/dsh-tools") or peer.get("@deepseek-ai/dsh") \
        or d.get("@deepseek-ai/dsh-tools") or d.get("@deepseek-ai/dsh")
    out.append({"name": name, "req": req, "has": bool(req)})

def plugin_compat_report(tools_version):
    """检测插件与指定 dsh-tools 版本的兼容性"""
    rows = []
    for it in installed_plugin_reqs():
        if not it["has"]:
            rows.append({"name": it["name"], "req": "", "ok": True,
                         "note": "未声明 dsh-tools 依赖，视为兼容"})
            continue
        ok = ver_satisfies(tools_version, it["req"])
        rows.append({"name": it["name"], "req": it["req"],
                     "ok": ok, "note": "兼容" if ok else "版本不匹配"})
    return rows

def _norm_win_path(p):
    """Windows 路径归一化：去掉 UNC 前缀并统一小写，用于 junction 目标比较"""
    s = os.path.normpath(str(p or ""))
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return s.lower()

def _is_junction(p):
    """Windows 上判断路径是否为 junction（os.path.islink 对部分 junction 返回 False）"""
    try:
        if os.path.islink(p):
            return True
        os.readlink(p)
        return True
    except OSError:
        return False

def fix_tools_alignment():
    """快速修复：确保 web profile 的 @deepseek-ai/dsh-tools 以 junction 指向全局副本。
    返回 (ok, log[])。"""
    log_l = []
    global_tools = os.path.join(dsh_pkg_dir() or "", "node_modules", "@deepseek-ai", "dsh-tools")
    local_tools = os.path.join(PROFILE_DIR, "node_modules", "@deepseek-ai", "dsh-tools")
    if not os.path.isdir(global_tools):
        return False, ["找不到全局 dsh-tools: " + global_tools]
    os.makedirs(os.path.dirname(local_tools), exist_ok=True)
    try:
        if _is_junction(local_tools):
            target = os.readlink(local_tools)
            if _norm_win_path(target) == _norm_win_path(global_tools):
                log_l.append("dsh-tools junction 已指向全局副本，无需修复")
                return True, log_l
            log_l.append("旧 junction 指向 %s，重建" % target)
            os.rmdir(local_tools)  # junction 直接 rmdir，不递归
        elif os.path.isdir(local_tools):
            dup = local_tools + ".dup"
            if os.path.exists(dup):
                shutil.rmtree(dup)
            os.rename(local_tools, dup)
            log_l.append("本地副本已备份为 dsh-tools.dup")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", local_tools, global_tools],
                           capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
        if r.returncode != 0:
            return False, log_l + ["junction 创建失败: " + (r.stdout or r.stderr).strip()]
        log_l.append("已重建 junction: %s -> %s" % (local_tools, global_tools))
        return True, log_l
    except Exception as e:
        return False, log_l + ["修复异常: " + str(e)]

def plugin_quick_fix():
    """一键修复：对齐 dsh-tools junction + 重新检测。返回报告 dict"""
    ok, logs = fix_tools_alignment()
    ver = get_global_tools_version()
    logs.append("当前 dsh 内嵌 dsh-tools: " + str(ver))
    rows = plugin_compat_report(ver)
    bad = [r for r in rows if not r["ok"]]
    logs.append("兼容插件 %d 个，不兼容 %d 个" % (len(rows) - len(bad), len(bad)))
    return {"ok": ok and not bad, "logs": logs, "tools": ver, "plugins": rows}

# ---------- 新人推荐插件包 ----------
NEWBIE_BUNDLE = [
    ("dshmarket", "可视化插件市场：浏览、搜索、一键安装社区插件，是入门 dsh 生态的入口"),
    ("dsh-context", "上下文仪表盘：看清对话上下文由什么构成、如何演化，更好驾驭长对话"),
    ("dsh-notification", "桌面 + Webhook 通知：任务完成、出错、需要确认时第一时间提醒，不用挂屏"),
    ("dsh-plugin-check", "插件体检：一键比对已装插件，发现坏包、过期版本与有风险的插件"),
    ("@liustack/modsearch", "免费免 Key 的网页/推特搜索与抓页，让没有原生联网的模型也能查资料"),
    ("@liustack/modlens", "视觉桥接（基于免费 Antigravity CLI）：纯文本模型也能看图 / OCR"),
]
NEWBIE_SPECS = [name + "@latest" for name, _ in NEWBIE_BUNDLE]

def ensure_pnpm():
    """确保 pnpm 可用：未安装则用 npm 全局安装。返回 (ok, msg)"""
    if shutil.which("pnpm"):
        return True, ""
    if not shutil.which("npm"):
        return False, "缺少 npm，无法安装 pnpm（请先安装 Node.js）"
    log("[pnpm] 未检测到 pnpm，执行 npm install -g pnpm")
    try:
        r = subprocess.run(["cmd", "/c", "npm", "install", "-g", "pnpm"],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and shutil.which("pnpm"):
            log("[pnpm] 安装成功")
            return True, ""
        return False, "pnpm 安装失败: " + (r.stdout or r.stderr or "")[-300:]
    except Exception as e:
        return False, "pnpm 安装异常: " + str(e)

def install_newbie_bundle():
    """后台安装新人推荐插件包到 web profile，并修复 dsh-tools 对齐"""
    ok, msg = ensure_pnpm()
    if not ok:
        log("[新人包] " + msg)
        _ini_set("newbie_bundle", "failed")
        return False
    log("[新人包] 开始安装: " + ", ".join(n for n, _ in NEWBIE_BUNDLE))
    try:
        r = subprocess.run(["cmd", "/c", "pnpm", "add"] + NEWBIE_SPECS,
                           cwd=PROFILE_DIR, capture_output=True, text=True, timeout=900,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log("[新人包] pnpm add 失败: " + (r.stdout or r.stderr or "")[-500:])
            _ini_set("newbie_bundle", "failed")
            return False
        fix_tools_alignment()
        _ini_set("newbie_bundle", "installed")
        log("[新人包] 安装完成")
        return True
    except Exception as e:
        log("[新人包] 安装异常: " + str(e))
        _ini_set("newbie_bundle", "failed")
        return False

def ask_newbie_bundle_loop():
    """新环境首启：等 web profile 就绪后弹窗介绍推荐插件包，让用户自选安装"""
    try:
        if _ini_get("newbie_bundle", "") in ("installed", "skip"):
            return
        # 等待 dsh 首次 web 启动生成 profile（pnpm install 完成），最多 5 分钟
        tools_dir = os.path.join(PROFILE_DIR, "node_modules", "@deepseek-ai", "dsh-tools")
        waited = 0
        while not os.path.isdir(tools_dir) and waited < 300:
            time.sleep(5)
            waited += 5
        if not os.path.isdir(tools_dir):
            log("[新人包] 等待 profile 超时，跳过推荐")
            return
        time.sleep(10)  # 等 pnpm install 收尾（避免 lockfile 争用）
        import tkinter as tk
        from tkinter import messagebox
        desc = "\n\n".join("• %s —— %s" % (n, d) for n, d in NEWBIE_BUNDLE)
        root = tk.Tk()
        root.withdraw()
        ans = messagebox.askyesnocancel(
            "DSH Dock · 新人推荐插件包",
            "检测到首次使用 dsh，这里有 6 个社区验证过的常用插件，建议直接安装：\n\n"
            + desc + "\n\n"
            "选「是」→ 立即安装推荐包（pnpm add，需联网，约几分钟）\n"
            "选「否」→ 不装，之后可在插件市场自行安装\n"
            "选「取消」→ 暂不决定，下次启动再问",
            default=messagebox.YES, parent=root)
        root.destroy()
        if ans is True:
            _ini_set("newbie_bundle", "installing")
            threading.Thread(target=install_newbie_bundle, daemon=True).start()
        elif ans is False:
            _ini_set("newbie_bundle", "skip")
    except Exception as e:
        log("[新人包] 询问流程异常: " + str(e))

# ---------- 手机连接（LAN 访问 default web） ----------
WEBSERVER_PATCH_BLOCK = """- id: webserver
  config:
    host: '0.0.0.0'
    port: !!js ctx.webStartup.port ?? 3080
    compression: gzip
    compressionLevel: 1
    compressionThresholdBytes: 1024
"""

def _patch_has_webserver():
    try:
        txt = open(os.path.join(PROFILE_DIR, "cordis.patch.yml"), encoding="utf-8").read()
    except Exception:
        return False
    return bool(re.search(r"(?m)^- id: webserver\s*$", txt))

def _patch_set_webserver():
    """在 profile 的 cordis.patch.yml 追加 webserver host 段（幂等），让 web 监听所有网卡"""
    p = os.path.join(PROFILE_DIR, "cordis.patch.yml")
    try:
        txt = open(p, encoding="utf-8").read()
    except Exception:
        return False
    if _patch_has_webserver():
        return True
    if txt and not txt.endswith("\n"):
        txt += "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt + "\n" + WEBSERVER_PATCH_BLOCK)
    return True

def _patch_remove_webserver():
    """移除 patch 中的 webserver 段，恢复 web 默认 loopback 绑定"""
    p = os.path.join(PROFILE_DIR, "cordis.patch.yml")
    try:
        txt = open(p, encoding="utf-8").read()
    except Exception:
        return True
    txt2 = re.sub(r"(?ms)^- id: webserver\s*\n\s+config:.*?(?=^- id:|\Z)", "", txt)
    txt2 = txt2.rstrip("\n") + "\n"
    if txt2 != txt:
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt2)
    return True

def _lan_ips():
    """枚举本机局域网 IPv4 地址（非回环、非 APIPA）；失败时按出口网卡推断"""
    ips = []
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = a[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
            s.close()
        except Exception:
            pass
    return ips

def _swap_url_host(url, host, port):
    m = re.match(r"^(https?://)([^/:]+)(?::\d+)?(/.*)?$", url or "")
    if not m:
        return url
    return "%s%s:%s%s" % (m.group(1), host, port, m.group(3) or "/")

def _default_inst():
    for i in INSTANCES:
        if i.name == "default":
            return i
    return INSTANCES[0] if INSTANCES else None

def set_mobile(on, timeout=35):
    """开启/关闭手机连接（把 default 实例的 dsh web 开放到局域网）。
    返回 {ok, on, url, local_url, error}"""
    inst = _default_inst()
    if inst is None:
        return {"ok": False, "on": False, "error": "没有可用的实例"}
    try:
        if on:
            if not _patch_set_webserver():
                return {"ok": False, "on": False, "error": "无法修改 web profile 的 cordis.patch.yml"}
        else:
            _patch_remove_webserver()
        # patch 只对重启后的进程生效
        inst.capture = True
        inst.lan_url = None
        inst.stop()
        inst.start()
        if inst.status() != "running":
            return {"ok": False, "on": on, "error": "实例未能启动（端口被占用？）"}
        if not on:
            return {"ok": True, "on": False, "url": None, "local_url": None}
        # 等待 dsh 打印 web 地址（含 LAN/token）
        deadline = time.time() + timeout
        while time.time() < deadline and inst.lan_url is None:
            time.sleep(0.5)
        if not inst.lan_url:
            return {"ok": False, "on": True, "error": "等待 web 地址超时，请查看 DSH.log"}
        url = inst.lan_url
        ips = _lan_ips()
        if ips:
            url = _swap_url_host(url, ips[0], inst.port)
            inst.lan_url = url
        local_url = _swap_url_host(url, "127.0.0.1", inst.port)
        return {"ok": True, "on": True, "url": url, "local_url": local_url}
    except Exception as e:
        return {"ok": False, "on": on, "error": "手机连接异常: %s" % e}

# ---------- 定时任务 ----------
CRON_FILE = os.path.join(BASE_DIR, "cron_tasks.json")
CRON_SCHED_LABELS = {"once": "一次性", "interval": "间隔(分钟)", "daily": "每天", "weekly": "每周"}
CRON_WDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

def _cron_init_runtime(t):
    t.setdefault("state", "idle")
    t.setdefault("last_run", None)
    t.setdefault("last_result", None)
    t.setdefault("last_error", None)
    t.setdefault("runs", 0)
    if not t.get("name"):
        t["name"] = t.get("id", "任务")
    t["log"] = collections.deque(maxlen=500)
    t["_lock"] = threading.Lock()
    t["pending"] = False
    return t

def _cron_load():
    try:
        with open(CRON_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for t in data:
                _cron_init_runtime(t)
                t["next_run"] = _cron_next(t)   # 重启后重算下次触发（跳过已错过的时刻）
            return data
    except Exception:
        pass
    return []

CRON_TASKS = _cron_load()

def _cron_save():
    try:
        out = []
        for t in CRON_TASKS:
            d = {k: v for k, v in t.items() if k not in ("log", "_lock", "pending", "proc")}
            out.append(d)
        with open(CRON_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("定时任务保存失败: " + str(e))

def _cron_fmt(ts):
    try:
        if not ts:
            return "—"
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "—"

def _cron_hhmm_next(hhmm, now=None):
    now = now or time.time()
    lt = time.localtime(now)
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        hh, mm = 9, 0
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, -1, -1))
    if target <= now:
        target += 86400
    return target

def _cron_weekday_next(wd, hhmm, now=None):
    """wd: 1=周一 … 7=周日"""
    now = now or time.time()
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, -1, -1))
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        hh, mm = 9, 0
    wd0 = int(wd) % 7
    delta = (wd0 - lt.tm_wday + 7) % 7
    target = base + delta * 86400 + hh * 3600 + mm * 60
    if target <= now:
        target += 7 * 86400
    return target

def _cron_next(t, now=None):
    """计算任务下次触发时间（epoch 秒）；未启用/无下次时返回 None"""
    now = now or time.time()
    if not t.get("enabled"):
        return None
    sched = t.get("schedule", "once")
    p = t.get("param") or {}
    if sched == "once":
        at = p.get("run_at")
        return at if isinstance(at, (int, float)) and at > now else None
    if sched == "interval":
        mins = max(1, int(p.get("interval_min") or 60))
        base = t.get("last_run")
        return (base or now) + mins * 60
    if sched == "daily":
        return _cron_hhmm_next(p.get("time", "09:00"), now)
    if sched == "weekly":
        return _cron_weekday_next(int(p.get("weekday") or 1), p.get("time", "09:00"), now)
    return None

def _cron_schedule_text(t):
    sched = t.get("schedule", "once")
    p = t.get("param") or {}
    if sched == "once":
        return "一次性 · " + _cron_fmt(p.get("run_at"))
    if sched == "interval":
        return "每 %s 分钟" % max(1, int(p.get("interval_min") or 60))
    if sched == "daily":
        return "每天 " + str(p.get("time", "09:00"))
    if sched == "weekly":
        wd = int(p.get("weekday") or 1)
        return "每周" + CRON_WDAY[(wd % 7) - 1] + " " + str(p.get("time", "09:00"))
    return sched

def _cron_inst_text(t):
    inst = _find_inst(t.get("inst") or "")
    if inst:
        return "%s(:%d)" % (inst.name, inst.port)
    return (t.get("inst") or "—")

def _cron_find(cid):
    for t in CRON_TASKS:
        if t.get("id") == cid:
            return t
    return None

_CRON_LAST_NEW_ID = [None]

def _cron_new_id():
    import uuid
    return "c_" + uuid.uuid4().hex[:8]

def cron_add(data):
    """新增定时任务。成功返回 None，失败返回错误信息"""
    name = (data.get("name") or "").strip()
    desc = (data.get("desc") or "").strip()
    if not name:
        return "任务名称不能为空"
    if not desc:
        return "任务需求不能为空"
    sched = data.get("schedule") or "once"
    if sched not in CRON_SCHED_LABELS:
        return "未知的调度形式: " + sched
    param = dict(data.get("param") or {})
    if sched == "interval":
        try:
            mins = int(param.get("interval_min") or 60)
        except (TypeError, ValueError):
            return "间隔分钟必须是整数"
        if mins < 1 or mins > 10080:
            return "间隔分钟须在 1-10080 之间"
    elif sched == "once":
        at = param.get("run_at")
        if not isinstance(at, (int, float)) or at <= time.time():
            return "一次性任务的执行时间需晚于当前时间"
    elif sched in ("daily", "weekly"):
        hhmm = str(param.get("time") or "")
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", hhmm):
            return "时间格式须为 HH:MM"
        if sched == "weekly":
            try:
                wd = int(param.get("weekday") or 0)
            except (TypeError, ValueError):
                wd = 0
            if not 1 <= wd <= 7:
                return "星期须在 1(周一)-7(周日) 之间"
    inst = (data.get("inst") or "default").strip()
    if not _find_inst(inst):
        return "实例不存在: " + inst
    ws = (data.get("workspace") or "").strip()
    if ws and not os.path.isdir(ws):
        return "工作目录不存在: " + ws
    t = {"id": _cron_new_id(), "name": name, "desc": desc, "schedule": sched,
         "param": param, "inst": inst, "workspace": ws, "enabled": True,
         "next_run": None}
    _cron_init_runtime(t)
    t["next_run"] = _cron_next(t)
    CRON_TASKS.append(t)
    _CRON_LAST_NEW_ID[0] = t["id"]
    _cron_save()
    log(f"[定时任务] 新增: {name} ({CRON_SCHED_LABELS[sched]}, 实例 {inst}, 目标 {ws or '实例工作区'})")
    return None

def cron_toggle(cid, enabled):
    t = _cron_find(cid)
    if not t:
        return "任务不存在"
    t["enabled"] = bool(enabled)
    if t["enabled"]:
        t["next_run"] = _cron_next(t)
    else:
        t["next_run"] = None
    _cron_save()
    log("[定时任务] %s -> %s" % (t["name"], "启用" if t["enabled"] else "停用"))
    return None

def cron_del(cid):
    t = _cron_find(cid)
    if not t:
        return "任务不存在"
    if t.get("state") == "running":
        return "任务正在运行，无法删除"
    CRON_TASKS.remove(t)
    _cron_save()
    log("[定时任务] 删除: " + t["name"])
    return None

# ---------- Windows 桌面通知（PowerShell NotifyIcon，无第三方依赖） ----------
def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"

def notify(title, text):
    """右下角气泡通知。异步执行，不阻塞主程序"""
    try:
        import tempfile
        ps1 = os.path.join(tempfile.gettempdir(), "dshdock_notify.ps1")
        body = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "Add-Type -AssemblyName System.Drawing\n"
            "$n = New-Object System.Windows.Forms.NotifyIcon\n"
            "$n.Text = 'DSH Dock'\n"
            "$n.Icon = [System.Drawing.SystemIcons]::Information\n"
            "$n.Visible = $true\n"
            "$n.BalloonTipTitle = %s\n"
            "$n.BalloonTipText = %s\n"
            "$n.ShowBalloonTip(10000)\n"
            "Start-Sleep -Seconds 12\n"
            "$n.Dispose()\n"
        ) % (_ps_quote(title), _ps_quote(text))
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(body)
        subprocess.Popen(["powershell", "-NoProfile", "-NoLogo", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File", ps1],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log("桌面通知失败: " + str(e))

# ---------- 定时任务执行 ----------
def run_cron_job(t):
    """按任务设定：拉起指定实例 → 在目标工作目录向 dsh 发送任务需求 → 流式监控 → 完成提醒"""
    t["proc"] = None
    if not t["_lock"].acquire(blocking=False):
        return
    try:
        t["pending"] = False
        now = time.time()
        inst = _find_inst(t.get("inst") or "")
        workdir = (t.get("workspace") or "").strip() or (inst.workspace if inst else "")
        if inst:
            inst.start()   # 任务开始时打开任务指定的 dsh 端口（若未运行），便于浏览器查看进度
        if not workdir:
            workdir = BASE_DIR
        try:
            os.makedirs(workdir, exist_ok=True)
        except OSError:
            pass
        node, bin_js = DSH or find_dsh()
        if not node:
            t["state"], t["last_error"] = "error", "找不到 dsh 命令"
            t["log"].append("[错误] 找不到 dsh 命令")
            _cron_save()
            notify("DSH Dock 定时任务失败", "%s\n找不到 dsh 命令" % t["name"])
            return
        t.update(state="running", last_run=now, last_error=None)
        t["log"].append("[%s] 开始执行，工作目录: %s" % (time.strftime("%H:%M:%S"), workdir))
        t["runs"] = (t.get("runs") or 0) + 1
        env = os.environ.copy()
        if inst:
            for k, v in inst.env.items():
                env[k] = v
            if inst.dshhome:
                env["DSH_HOME"] = inst.dshhome
        cmd = [node, bin_js, "--profile", "headless", t["desc"]]
        log("[定时任务] 执行 %s: %s (cwd=%s)" % (t["name"], t["desc"][:80], workdir))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=workdir, env=env)
        except Exception as e:
            t["state"], t["last_error"] = "error", "启动失败: %s" % e
            t["log"].append("[错误] 启动失败: %s" % e)
            _cron_save()
            notify("DSH Dock 定时任务失败", "%s\n%s" % (t["name"], t["last_error"]))
            return
        t["proc"] = proc
        try:
            for ln in proc.stdout:
                ln = ln.rstrip("\r\n")
                t["log"].append(ln)
        except Exception:
            pass
        rc = proc.wait()
        t["proc"] = None
        tail = list(t["log"])[-12:]
        summary = "\n".join(tail)[-400:]
        if rc == 0:
            t["state"] = "done"
            t["last_result"] = summary
            t["last_error"] = None
        else:
            t["state"] = "error"
            t["last_error"] = "退出码 %d" % rc
            t["last_result"] = summary
        t["next_run"] = _cron_next(t, time.time())
        if t.get("schedule") == "once" and rc == 0:
            t["enabled"] = False
            t["next_run"] = None
        _cron_save()
        log("[定时任务] 结束 %s: rc=%d → %s" % (t["name"], rc, t["state"]))
        if t["state"] == "done":
            notify("DSH Dock 定时任务完成", "%s 已完成\n%s" % (t["name"], _cron_fmt(now)))
        else:
            notify("DSH Dock 定时任务失败", "%s\n%s" % (t["name"], t["last_error"]))
    finally:
        t["_lock"].release()

def run_cron_job_async(cid):
    t = _cron_find(cid)
    if not t:
        return "任务不存在"
    if t.get("state") == "running":
        return "该任务正在运行中"
    threading.Thread(target=run_cron_job, args=(t,), daemon=True).start()
    return None

def cron_loop():
    """调度线程：周期扫描到期任务并拉起来执行"""
    while not stop_evt.is_set():
        try:
            now = time.time()
            for t in list(CRON_TASKS):
                nr = t.get("next_run")
                if (t.get("enabled") and nr and now >= nr and not t["pending"]
                        and t.get("state") != "running"):
                    t["pending"] = True
                    threading.Thread(target=run_cron_job, args=(t,), daemon=True).start()
        except Exception as e:
            log("定时任务调度异常: " + str(e))
        time.sleep(15)

def cron_serialize():
    out = []
    for t in CRON_TASKS:
        d = {k: v for k, v in t.items() if k not in ("log", "_lock", "pending", "proc")}
        d["tail"] = list(t["log"])[-14:]
        d["schedule_text"] = _cron_schedule_text(t)
        d["inst_text"] = _cron_inst_text(t)
        d["next_text"] = _cron_fmt(d.get("next_run"))
        d["last_text"] = _cron_fmt(d.get("last_run"))
        out.append(d)
    return out

_tools_versions_cache = None

def get_tools_versions():
    """registry 上 @deepseek-ai/dsh-tools 的全部版本号列表（缓存）"""
    global _tools_versions_cache
    if _tools_versions_cache is not None:
        return _tools_versions_cache
    try:
        req = urllib.request.Request("https://registry.npmjs.org/@deepseek-ai/dsh-tools",
                                     headers={"User-Agent": "dsh-launcher"})
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.load(r)
        _tools_versions_cache = list((meta.get("versions") or {}).keys())
    except Exception:
        _tools_versions_cache = []
    return _tools_versions_cache

def resolve_tools_version_for(range_spec):
    """给定范围，返回 registry 中满足该范围的最高 dsh-tools 版本（近似实际安装结果）"""
    best, kb = None, None
    for v in get_tools_versions():
        if not ver_satisfies(v, range_spec or "*"):
            continue
        k = _ver_key(v)
        if k is not None and (kb is None or _cmp_ver_key(k, kb) > 0):
            best, kb = v, k
    return best

def plugin_compat_full(ch):
    """ch: current | stable | beta | alpha
    返回 {target_dsh, tools_version, tools_required, rows}：目标 dsh 版本、对应 dsh-tools
    版本、插件兼容报告。用于更新前探测与当前状态检测。"""
    if ch == "current":
        tools = get_global_tools_version()
        return {"target_dsh": get_local_version(), "tools_version": tools,
                "tools_required": None, "rows": plugin_compat_report(tools or "0.0.0")}
    chs = get_dsh_channels()
    dsh_target = chs.get(ch) or chs.get("stable")
    if not dsh_target:
        return {"target_dsh": None, "tools_version": None, "tools_required": None,
                "rows": [], "error": "无法获取 %s 通道版本" % ch}
    tools_range = get_target_tools_range(dsh_target)
    tools = resolve_tools_version_for(tools_range) if tools_range else None
    if tools is None:
        # 依赖链解析失败时以当前内核工具版本近似（同一 0.1.x 系列结果一致）
        tools = get_global_tools_version()
        if tools_range:
            tools_range = str(tools_range) + "（未解析，以当前 %s 近似）" % tools
    return {"target_dsh": dsh_target, "tools_required": tools_range,
            "tools_version": tools, "rows": plugin_compat_report(tools or "0.0.0")}

# ---------- 版本 ----------
ver_local = None
ver_latest = None
upgrading = False

def get_local_version():
    if not DSH:
        return None
    node, bin_js = DSH
    try:
        r = subprocess.run([node, bin_js, "--version"], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
    except Exception:
        return None

def get_latest_version():
    try:
        with urllib.request.urlopen(REGISTRY_URL, timeout=10) as resp:
            return json.load(resp)["version"]
    except Exception:
        return None

def version_check_loop():
    global ver_local, ver_latest
    if not DSH:
        return
    ver_local = get_local_version()
    ver_latest = get_latest_version()
    if ver_latest and ver_latest != ver_local:
        log(f"发现 dsh 新版本: 本地 {ver_local} -> {ver_latest}")
    update_ui()

def do_upgrade(icon, item):
    global upgrading
    if upgrading or not ver_latest:
        return
    upgrading = True
    update_ui()
    icon.notify(f"正在升级 dsh 到 {ver_latest} ...", APP_NAME)
    def work():
        global upgrading, ver_latest
        try:
            subprocess.run(["cmd", "/c", "npm", "install", "-g", "@deepseek-ai/dsh@latest"],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=600)
            icon.notify("升级完成，重启实例后生效", APP_NAME)
            ver_latest = get_latest_version()
        except Exception as e:
            log(f"升级失败: {e}")
            icon.notify("升级失败，见 DSH.log", APP_NAME)
        finally:
            upgrading = False
            update_ui()
    threading.Thread(target=work, daemon=True).start()

# ---------- 管理界面版本检查 / 更新（dsh 本体 + dshmarket） ----------
_upd_lock = threading.Lock()
_upd_state = {"kind": None, "running": False, "done": False, "ok": False, "target": ""}

def _get_market_local_version():
    """dshmarket 实际安装版本（读 node_modules 的 package.json）"""
    try:
        p = os.path.join(PROFILE_DIR, "node_modules", "dshmarket", "package.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None

def _get_market_latest_version():
    try:
        with urllib.request.urlopen("https://registry.npmjs.org/dshmarket/latest", timeout=10) as resp:
            return json.load(resp).get("version")
    except Exception:
        return None

def upd_check(kind, ch="stable"):
    """检查更新：返回当前/目标通道/可否更新等展示信息；更新任务进行中/已结束时返回任务状态"""
    global ver_latest
    # 该通道有进行中或刚结束的任务 → 返回任务状态（供前端轮询判定 done/ok）
    with _upd_lock:
        if _upd_state["kind"] == kind and _upd_state["running"]:
            return {"busy": True, "done": False, "ok": False, "msg": "更新进行中…", "target": _upd_state["target"]}
        if _upd_state["kind"] == kind and _upd_state["done"]:
            msg = "更新完成，重启实例后生效" if _upd_state["ok"] else "更新失败，见 DSH.log"
            return {"busy": False, "done": True, "ok": _upd_state["ok"], "msg": msg, "target": "", "updatable": False}
    if kind == "dsh":
        cur = get_local_version()
        channels = get_dsh_channels()
        latest = channels.get(ch) or channels.get("stable") or ver_latest
        if latest and ver_latest != latest:
            ver_latest = latest
        if not latest or not cur:
            return {"busy": False, "done": True, "ok": bool(cur), "msg": "无法获取版本（dsh 未安装？）",
                    "updatable": False, "channels": channels, "chosen": ch}
        updatable = cur != latest
        msg = f"当前 {cur} · {ch}通道 {latest}" + ("，有新版本可更新" if updatable else "，已是最新")
        return {"busy": False, "done": False, "updatable": updatable, "target": latest if updatable else "",
                "msg": msg, "channels": channels, "chosen": ch}
    else:
        cur = _get_market_local_version()
        latest = _get_market_latest_version()
        if not latest or not cur:
            return {"busy": False, "done": True, "ok": bool(cur), "msg": "无法获取 dshmarket 版本", "updatable": False}
        updatable = cur != latest
        msg = f"当前 dshmarket {cur} · 最新 {latest}" + ("，有新版本可更新" if updatable else "，已是最新")
        return {"busy": False, "done": False, "updatable": updatable, "target": latest if updatable else "", "msg": msg}

def upd_start(kind, ch="stable"):
    """启动后台更新，返回 None 或错误信息"""
    if kind not in ("dsh", "mkt"):
        return "未知更新类型"
    with _upd_lock:
        if _upd_state["running"]:
            return "已有更新任务进行中"
        _upd_state.update({"kind": kind, "running": True, "done": False, "ok": False, "target": ch})
    threading.Thread(target=_upd_worker, args=(kind, ch), daemon=True).start()
    log(f"[更新] 开始更新 {kind} (通道 {ch})")
    return None

def _upd_worker(kind, ch="stable"):
    try:
        if kind == "dsh":
            channels = get_dsh_channels()
            tag = channels.get(ch) or "latest"
            cmd = ["cmd", "/c", "npm", "install", "-g", "@deepseek-ai/dsh@%s" % tag]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = r.returncode == 0
            if ok:
                log(f"[更新] dsh 本体升级到 {tag} 完毕")
            else:
                log(f"[更新] dsh 升级失败: {(r.stdout or '')[-500:]}{(r.stderr or '')[-500:]}")
        else:
            # dshmarket 更新：先确保 pnpm 可用，再关掉 pnpm 11 的发布冷却检查
            # （顶层 minimumReleaseAge: 0），修复 lockfile 后升级，避免 lockfile 损坏 / 新版本冷却期导致失败
            ok_m, msg_m = ensure_pnpm()
            if not ok_m:
                log("[更新] dshmarket 更新跳过: " + msg_m)
                raise RuntimeError("pnpm 不可用: " + msg_m)
            ws_file = os.path.join(PROFILE_DIR, "pnpm-workspace.yaml")
            try:
                if os.path.isfile(ws_file):
                    ws = open(ws_file, encoding="utf-8").read()
                    if "minimumReleaseAge:" not in ws:
                        open(ws_file, "a", encoding="utf-8").write("\nminimumReleaseAge: 0\n")
            except Exception as e:
                log(f"[更新] 写 pnpm-workspace 失败: {e}")
            subprocess.run(["cmd", "/c", "pnpm", "install", "--no-frozen-lockfile"],
                           cwd=PROFILE_DIR, capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            r2 = subprocess.run(["cmd", "/c", "pnpm", "add", "dshmarket@latest"],
                                cwd=PROFILE_DIR, capture_output=True, text=True, timeout=900,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = r2.returncode == 0
            if not ok:
                log(f"[更新] dshmarket 更新失败: {r2.stdout[-500:]}{r2.stderr[-500:]}")
            else:
                log("[更新] dshmarket 已升级")
    except Exception as e:
        log(f"[更新] {kind} 更新异常: {e}")
        ok = False
    with _upd_lock:
        _upd_state.update({"running": False, "done": True, "ok": ok})
    update_ui()

# ---------- 托盘 ----------
icon = None
stop_evt = threading.Event()

# win32 通知消息常量（pystray._util.win32 中定义）
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

def _web_ready(port, http_url, timeout=4):
    """探测一个 dsh web 端口是否真正可用：TCP 已监听且 HTTP 有响应。
    注意 dsh web 对未带 token 的访问返回 401，因此只要求服务器有 HTTP 响应
    （状态码 < 500）即视为就绪，浏览器打开后由页面自身引导登录。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
    except Exception:
        return False
    try:
        return urllib.request.urlopen(http_url, timeout=timeout).status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False

def open_instance_page(inst, wait_timeout=25):
    """打开实例页面：先轮询等待网页就绪（避免刚启动时“找不到此页”），
    打开后若仍未连通，由看护线程在服务可用时再次打开（浏览器会复用标签页）"""
    url = "http://127.0.0.1:%d" % inst.port
    ready = _web_ready(inst.port, url)
    t0 = time.time()
    while not ready and time.time() - t0 < wait_timeout:
        time.sleep(0.6)
        ready = _web_ready(inst.port, url)
    webbrowser.open(url)
    log("[打开页面] %s 就绪=%s" % (url, ready))
    if not ready:
        def _watch():
            start = time.time()
            while not stop_evt.is_set() and time.time() - start < 40:
                if _web_ready(inst.port, url):
                    webbrowser.open(url)
                    log("[打开页面] 服务已就绪，重新打开 %s" % url)
                    return
                time.sleep(2)
            log("[打开页面] 等待 %s 超时，请手动刷新" % url)
        threading.Thread(target=_watch, daemon=True).start()

def open_default_page():
    """打开 default（3080）实例的 dsh 页面；无 default 时打开第一个实例"""
    target = None
    for i in INSTANCES:
        if i.name == "default":
            target = i
            break
    if target is None and INSTANCES:
        target = INSTANCES[0]
    if target:
        open_instance_page(target)

class TrayIcon(pystray.Icon):
    """扩展托盘图标：
    - 单击 → 打开管理界面
    - 双击 → 打开 default 实例页面
    - 右键弹出菜单前先重建菜单，保证状态实时刷新
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_lclick = 0.0
        self._single_pending = False

    def _on_notify(self, wparam, lparam):
        if lparam == WM_LBUTTONUP:
            self._on_left_click()
        elif lparam == WM_RBUTTONUP:
            update_ui()  # 弹出前重建菜单，状态永远是最新
            super()._on_notify(wparam, lparam)
        else:
            super()._on_notify(wparam, lparam)

    def _on_left_click(self):
        try:
            dbl = ctypes.windll.user32.GetDoubleClickTime() / 1000.0
        except Exception:
            dbl = 0.3
        now = time.monotonic()
        if self._last_lclick and now - self._last_lclick <= dbl:
            # 双击：打开 default 实例页面
            self._last_lclick = 0.0
            self._single_pending = False
            open_default_page()
        else:
            # 单击：延迟等双击窗口，未双击则打开管理界面
            self._last_lclick = now
            self._single_pending = True
            threading.Timer(dbl, self._fire_single_click).start()

    def _fire_single_click(self):
        if self._single_pending:
            self._single_pending = False
            webbrowser.open(f"http://127.0.0.1:{ADMIN_PORT}")

def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 63, 63], radius=12, fill=(77, 107, 254, 255))
    font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 34) if os.path.exists(r"C:\Windows\Fonts\arialbd.ttf") else ImageFont.load_default()
    text = "DSH"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1]), text, font=font, fill=(255, 255, 255, 255))
    return img

def update_ui():
    if icon is not None:
        icon.menu = build_menu()
        icon.update_menu()

def _act(inst, method):
    """pystray action 回调工厂：固定实例引用，避免带默认参数的 lambda"""
    def action(icon, item):
        getattr(inst, method)()
        update_ui()
    return action

def _act_embed(inst):
    def action(icon, item):
        err = open_embedded(inst.name)
        if err:
            icon.notify(err, APP_NAME)
    return action

def _act_tui(inst):
    def action(icon, item):
        err = open_tui(inst.name)
        if err:
            icon.notify(err, APP_NAME)
    return action

def _enabled_stop(inst):
    def enabled(item):
        # external 状态同样允许停止：按端口结束占用进程
        return inst.status() in ("running", "external")
    return enabled

def _toggle_auto(icon, item):
    set_autostart(not autostart_enabled())
    update_ui()

def build_menu():
    items = []

    def open_admin(i, item):
        webbrowser.open(f"http://127.0.0.1:{ADMIN_PORT}")
    def open_def(i, item):
        open_default_page()

    def start_all(i, item):
        for x in INSTANCES:
            x.start()
        update_ui()
    def stop_all(i, item):
        for x in INSTANCES:
            x.stop()
        update_ui()

    items.append(pystray.MenuItem("打开管理界面", open_admin))
    items.append(pystray.MenuItem("打开 default 页面", open_def))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("全部启动", start_all))
    items.append(pystray.MenuItem("全部停止", stop_all))
    items.append(pystray.MenuItem("开机自启", _toggle_auto, checked=lambda item: autostart_enabled()))
    items.append(pystray.Menu.SEPARATOR)

    for inst in INSTANCES:
        # 每个实例一个独立子菜单（右键自动收起一组操作）
        running = inst.status() in ("running", "external")
        sub_items = [
            pystray.MenuItem("打开页面", _act(inst, "open_web")),
            pystray.MenuItem("应用内打开", _act_embed(inst)),
            pystray.MenuItem("TUI 终端", _act_tui(inst)),
            pystray.Menu.SEPARATOR,
        ]
        if running:
            sub_items.append(pystray.MenuItem("停止", _act(inst, "stop"), enabled=_enabled_stop(inst)))
        else:
            sub_items.append(pystray.MenuItem("启动", _act(inst, "start")))
        sub_items.append(pystray.MenuItem("打开管理界面", open_admin))
        sub = pystray.Menu(*sub_items)
        # 子菜单标题实时显示状态
        items.append(pystray.MenuItem(
            lambda item, x=inst: f"{x.name}  (: {x.port})  {STATUS_TEXT.get(x.status(), x.status())}",
            sub))

    if ver_latest and ver_latest != ver_local:
        vtext = f"发现新版本: {ver_local} -> {ver_latest}"
        items.append(pystray.MenuItem(vtext, None, enabled=False))
        items.append(pystray.MenuItem("一键升级 dsh", do_upgrade, enabled=lambda item: not upgrading))
    else:
        items.append(pystray.MenuItem(f"dsh 版本: {ver_local or '未知'}", None, enabled=False))

    def quit_app(i, item):
        log("=== 退出托盘守护 ===")
        stop_evt.set()
        for x in INSTANCES:
            x.stop()
        icon.stop()
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("退出", quit_app))
    return pystray.Menu(*items)

# ---------- 主流程 ----------
def _run_embedded(url):
    """子进程入口：主线程跑 webview 窗口，关闭后自动退出（不加载托盘/配置）"""
    import webview
    webview.create_window("DSH Dock 内嵌窗口", url, width=1280, height=820)
    webview.start()

def main():
    global DSH
    # 内嵌窗口子进程：开窗即走此分支，避免在托盘/HTTP 线程里跑 GUI
    embed_url = os.environ.pop("DSH_EMBED_URL", None)
    if embed_url:
        try:
            _run_embedded(embed_url)
        except Exception as e:
            log("内嵌窗口异常: " + str(e))
        return
    # 双击 exe 时的互斥检测：管理界面(3999)已在运行说明守护已驻留，只打开管理界面
    if is_open(ADMIN_PORT):
        webbrowser.open(f"http://127.0.0.1:{ADMIN_PORT}")
        return

    log("=== DSH Dock 托盘守护启动 ===")
    if not DSH:
        # 新环境引导：弹窗让用户选择手动提供 dsh 路径或由 Dock 协助安装
        DSH = bootstrap_dsh()
        if not DSH:
            log("未提供 dsh 安装，程序退出")
            return
        log("dsh 路由成功: " + DSH[1])
    load_config()
    start_admin_server()
    # 只自动启动 default 实例，其他实例需手动开启
    for inst in INSTANCES:
        if inst.name == "default" and inst.auto_start:
            inst.start()
    for inst in INSTANCES:
        threading.Thread(target=inst.monitor, daemon=True).start()
    threading.Thread(target=version_check_loop, daemon=True).start()
    threading.Thread(target=cron_loop, daemon=True).start()
    if not os.environ.get("DSH_TEST"):
        # 新环境首启：等 web profile 就绪后弹窗询问是否安装新人推荐插件包
        threading.Thread(target=ask_newbie_bundle_loop, daemon=True).start()
    if not os.environ.get("DSH_TEST"):
        # 首次启动自动打开的页面：默认管理界面；可在设置里改为 default 工作页
        if get_startup_page() == "admin":
            webbrowser.open(f"http://127.0.0.1:{ADMIN_PORT}")
            log("首次启动：打开管理界面 http://127.0.0.1:%d" % ADMIN_PORT)
        else:
            open_default_page()  # 打开 default 工作页（内置就绪等待 + 未连通自动刷新）

    if os.environ.get("DSH_TEST"):
        # 测试模式：不进托盘，跑 N 秒（每 5 秒记录状态）后停止（用于无人值守验证）
        n = int(os.environ.get("DSH_TEST_SEC", "30"))
        log(f"--- 测试模式运行 {n} 秒 ---")
        for i in range(n):
            time.sleep(1)
            if i % 5 == 0:
                log("状态: " + " | ".join(f"{x.name}={x.status()}(:{x.port})" for x in INSTANCES))
        for inst in INSTANCES:
            inst.stop()
        log("--- 测试模式结束 ---")
        return

    global icon
    icon = TrayIcon("DSH Dock", make_tray_image(), APP_NAME, build_menu())
    try:
        icon.run()
    except Exception as e:
        import traceback
        log("托盘异常: " + repr(e))
        log(traceback.format_exc())
        raise
    log("=== DSH Dock 托盘守护已退出 ===")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("启动器异常:\n" + traceback.format_exc())
        raise
