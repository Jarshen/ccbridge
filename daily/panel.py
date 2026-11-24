"""Win1 原生会话配对面板。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import quote, urlsplit
import webbrowser
from uuid import UUID

if __package__:
    from . import project_hooks
else:
    import project_hooks


OFFLINE = "可先保存配对。请从日常启动入口启动带桥服务。启动前请先关闭普通 CloudCLI 服务，待桥服务就绪后再试用。"
KINDS = ("cc", "desktop")
NAMES = {"cc": "Claude Code", "desktop": "Codex Desktop"}
STATES = {
    "pending": "待处理", "queued": "排队中", "submitting": "提交中",
    "submitted": "已提交", "received": "已接收", "running": "处理中",
    "returning": "回传中", "returned": "已回传", "delivered": "已送达",
    "completed": "已完成", "done": "已完成", "replied": "已回复",
    "failed": "失败", "error": "错误", "unknown": "结果待核对", "not_sent": "未发出",
    "unresolved": "待人工处理", "resolved": "已人工处理", "cancelled": "已取消",
    "awaiting_pickup": "待会话领取", "notified": "已通知", "awaiting_reply": "等待答复",
}
REPLY_MODES = {"required": "需要回复", "none": "仅通知", "deferred": "稍后答复"}
PENDING = {"pending", "queued", "submitting", "submitted", "received", "running", "returning"}
ISSUES = {"failed", "error", "unknown", "unresolved", "not_sent"}
RECONCILABLE = {"submitting", "submitted", "received", "returning", "failed", "unknown", "awaiting_reply"}


def loopback_url(value):
    try:
        url = urlsplit(value)
        host = url.hostname
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        if (url.scheme != "http" or not local or url.username is not None
                or url.query or url.fragment or url.port == 0
                or any(c.isspace() or c == "\\" for c in value)):
            raise ValueError
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("请填写本机 HTTP 回环地址，例如 http://127.0.0.1:3001") from None
    return value.rstrip("/")


def worker_status(path):
    if not path:
        return False, "未配置服务状态文件", OFFLINE
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        age = time.time() - float(data["heartbeat"])
        state = str(data["state"]).lower()
        fresh = math.isfinite(age) and -1 <= age <= 15
        ready = fresh and state in {"ready", "online", "running"}
        text = ("服务就绪" if ready else "服务未就绪") if fresh else "服务心跳已过期"
        if state == "offline":
            text = "服务离线"
        info = (f"{data.get('message') or state}  ·  PID {data['pid']}  ·  "
                f"端口 {data['port']}  ·  实例 {data['instance']}")
        return ready, text, info if ready else f"{OFFLINE}  {info}"
    except FileNotFoundError:
        return False, "服务离线", OFFLINE
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return False, "无法读取服务状态", f"{OFFLINE}  {exc}"


def display_state(mail):
    if mail["state"] == "unknown" and mail.get("detail") == "配对收件箱待领取":
        return "awaiting_pickup"
    return mail["state"]


def receipt_label(mail):
    stage=mail.get('receipt')
    labels={'registered':'已登记','native_queued':'已入队','delivered':'已送达','not_sent':'未发出'}
    state=display_state(mail)
    if stage in labels:
        label=labels[stage]
        if stage=='not_sent' and state=='resolved':
            return label+' · 已处理'
        if state=='queued':
            return label+' · 待投递'
        if state in ('awaiting_reply','returning','returned','awaiting_pickup','unknown','failed'):
            return label+' · '+STATES.get(state,state)
        return label
    return STATES.get(state,state)


def display_time(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError, OverflowError):
        return str(value or "—")


def title(row):
    return row.get("title") or "未命名会话"


def describe(cc, desktop):
    return (f"Claude Code · {title(cc)}\n会话 ID　{cc['id']}\n"
            f"原生 ID　{cc.get('native_id') or '尚未记录'}\n目录　{cc.get('cwd') or '未记录'}\n"
            f"Codex Desktop · {title(desktop)}\n线程 ID　{desktop['native_id']}\n"
            f"目录　{desktop.get('cwd') or '未记录'}")


def endpoints(binding):
    return tuple(dict(id=binding[k], title=binding.get(k + "_title"),
                      cwd=binding.get(k + "_cwd"),
                      native_id=binding.get("native_cc") if k == "cc" else binding[k])
                 for k in KINDS)


def native_key(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return value


def conflicts(bindings, cc, desktop):
    return {str(b["id"]): b for b in bindings
            if b["cc"] == cc["id"] or b["native_cc"] == native_key(cc["native_id"])
            or b["desktop"] == native_key(desktop["native_id"])}


class Backend:
    """All catalog and Store calls run serially on one background thread."""

    def __init__(self, args, catalog, store_type, provision_module=None):
        self.args, self.catalog, self.store_type = args, catalog, store_type
        self.provision = provision_module
        self.store = None

    def bindings(self):
        return [b for b in self.store.bindings() if b["active"]]

    def snapshot(self, queries):
        result, errors = {}, {}
        for kind, query in zip(KINDS, queries):
            try:
                result[kind] = self.catalog(kind, getattr(self.args, kind + "_db"), query)
                result[kind+"_all"] = self.catalog(kind, getattr(self.args, kind + "_db"), "") if query else result[kind]
            except Exception as exc:
                errors[kind] = str(exc)
        try:
            if self.store is None:
                self.store = self.store_type(self.args.store)
            result["bindings"] = self.bindings()
        except Exception as exc:
            errors["bindings"] = str(exc)
        try:
            if self.store is not None:
                result["messages"] = self.store.receipts(Path(self.args.desktop_db).with_name('thread_history_1.sqlite'))
                result["cleanup"] = callable(getattr(self.store, "reconcile", None))
            else:
                errors["messages"] = errors["bindings"]
        except Exception as exc:
            errors["messages"] = str(exc)
        # 获取provision请求列表
        result["requests"] = []
        if self.provision:
            try:
                result["requests"] = self.provision.list_requests(self.args.store)
                result["drafts"] = self.provision.list_desktop_drafts(self.args.store)
            except Exception as exc:
                errors["requests"] = str(exc)
        result["worker"] = worker_status(self.args.status_file)
        return result, errors

    def bind(self, cc, desktop, expected):
        if conflicts(self.bindings(), cc, desktop) != expected:
            raise ValueError("相关配对已变更，请刷新列表并重新确认。")
        return self.store.bind(cc, desktop, expected_conflicts=expected.keys())

    def unbind(self, binding):
        if binding not in self.bindings():
            raise ValueError("这组配对已变更，请刷新列表并重新确认。")
        self.store.unbind(binding["id"])
        project = Path(binding["desktop_cwd"]).resolve()
        if (project_hooks.has_receipt_hooks(project, self.args.store) and
                not any(Path(b["desktop_cwd"]).resolve() == project for b in self.bindings())):
            return project

    def disable_unused_hooks(self, project):
        with self.store.unpaired_project(project):
            project_hooks.set_enabled(project, self.args.store, False)

    def setup_hooks(self, binding):
        project = Path(binding["desktop_cwd"])
        if binding not in self.bindings() or not project.is_absolute() or not project.is_dir():
            raise ValueError("所选活动配对或项目目录已变更，请刷新后重试。")
        native = self.catalog("desktop", self.args.desktop_db, binding["desktop"])
        if not any(r["native_id"] == binding["desktop"] and Path(r["cwd"]).samefile(project) for r in native):
            raise ValueError("Desktop 会话的实际工作目录与配对记录不符。")
        path = project_hooks.install(project, self.args.store)
        try:
            project_hooks.set_enabled(project, self.args.store, True)
        except project_hooks.ReviewRequired:
            project_hooks.open_review(project)
            return f"{path}\n\n在打开的 Codex CLI 输入 /hooks，审核并启用两条 inbox.py 收件命令。"
        return f"{path}\n\n两条收件命令均已信任并启用。在 Desktop 新回合中使用。"

    def create_request(self, desktop_id, title):
        """新建Claude配对请求"""
        if not self.provision:
            raise RuntimeError("provision模块未加载")
        from uuid import uuid4
        request_id = str(uuid4())
        return self.provision.request_for_desktop(
            self.args.store, desktop_id, request_id, title
        )

    def check_request(self, request_id):
        """检查配对请求进度"""
        if not self.provision:
            raise RuntimeError("provision模块未加载")
        return self.provision.get_request(self.args.store, request_id)


def confirmation(root, heading, prompt, needs_note):
    dialog = tk.Toplevel(root)
    dialog.title(heading)
    dialog.transient(root)
    dialog.resizable(True, True)
    content = ttk.Frame(dialog, padding=14)
    content.pack(expand=True, fill="both")
    ttk.Label(content, text=heading, style="Title.TLabel").pack(anchor="w", pady=(0, 8))
    page = ttk.Frame(content)
    page.pack(fill="both", expand=True)
    body = tk.Text(page, height=min(16, prompt.count("\n") + 5), width=65, wrap="word",
                   font="TkDefaultFont", padx=9, pady=8, relief="solid", borderwidth=1)
    scroll = ttk.Scrollbar(page, command=body.yview)
    body.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    body.pack(fill="both", expand=True)
    body.insert("1.0", prompt)
    body.configure(state="disabled")
    note = tk.StringVar(master=dialog)
    if needs_note:
        ttk.Label(content, text="核对说明（必填）").pack(anchor="w", pady=(8, 2))
        ttk.Entry(content, textvariable=note).pack(fill="x")
    buttons = ttk.Frame(content)
    buttons.pack(fill="x", pady=(12, 0))
    answer = None

    def accept():
        nonlocal answer
        answer = note.get().strip()
        dialog.destroy()

    yes = ttk.Button(buttons, text=heading, command=accept)
    yes.pack(side="right")
    cancel = ttk.Button(buttons, text="取消", command=dialog.destroy)
    cancel.pack(side="right", padx=8)
    if needs_note:
        yes.state(["disabled"])
        note.trace_add("write", lambda *_: yes.state(["!disabled" if note.get().strip() else "disabled"]))
    dialog.bind("<Escape>", lambda _: dialog.destroy())
    dialog.update_idletasks()
    x = max(0, root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2)
    y = max(0, root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 2)
    dialog.geometry(f"+{x}+{y}")
    dialog.grab_set()
    cancel.focus_set()
    root.wait_window(dialog)
    return answer


def tree_table(parent, columns, height):
    frame = ttk.Frame(parent)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        selectmode="browse", height=height)
    for key, label, width in columns:
        tree.heading(key, text=label, anchor="w")
        tree.column(key, width=width, minwidth=60, anchor="w")
    tree.grid(row=0, column=0, sticky="nsew")
    for orientation in ("vertical", "horizontal"):
        vertical = orientation == "vertical"
        bar = ttk.Scrollbar(frame, orient=orientation,
                            command=tree.yview if vertical else tree.xview)
        tree.configure(**{("yscrollcommand" if vertical else "xscrollcommand"): bar.set})
        bar.grid(row=0 if vertical else 1, column=1 if vertical else 0,
                 sticky="ns" if vertical else "ew")
    return frame, tree


def sync_rows(tree, rows):
    """Retain widget items, focus and scroll position across ordinary polling."""
    old = tree.get_children()
    order = tuple(rows)
    for key in old:
        if key not in rows:
            tree.delete(key)
    for i, (key, (values, tags)) in enumerate(rows.items()):
        if not tree.exists(key):
            tree.insert("", i, iid=key, values=values, tags=tags)
        else:
            tree.item(key, values=values, tags=tags)
            if old != order:
                tree.move(key, "", i)


class Picker(ttk.LabelFrame):
    def __init__(self, parent, kind, changed, search, launch):
        super().__init__(parent, text=NAMES[kind], padding=(12, 8, 12, 12))
        self.rows = {}
        self.query = tk.StringVar()
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text="搜索").grid(row=0, column=0, padx=(0, 8))
        entry = ttk.Entry(self, textvariable=self.query)
        entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(self, text="清空", width=5, command=lambda: self.query.set("")).grid(
            row=0, column=2, padx=(6, 0))
        table, self.tree = tree_table(self, [("title", "会话标题", 210),
                                             ("cwd", "工作目录", 185)], 7)
        table.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        self.note = ttk.Label(self, style="Muted.TLabel", wraplength=400)
        self.note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.fields = {}
        for index, (key, label) in enumerate((("id", "会话 ID"), ("native_id", "原生 ID"),
                                               ("cwd", "工作目录")), 3):
            ttk.Label(self, text=label).grid(row=index, column=0, sticky="w", padx=(0, 8))
            field = self.fields[key] = tk.StringVar()
            ttk.Entry(self, textvariable=field, state="readonly").grid(
                row=index, column=1, columnspan=2, sticky="ew", pady=1)
        self.open = ttk.Button(self, text="在浏览器打开 CC" if kind == "cc" else "在 Desktop 打开",
                               command=launch)
        self.open.grid(row=6, column=0, columnspan=3, sticky="w", pady=(9, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda _: changed())
        self.query.trace_add("write", lambda *_: search())

    def current(self):
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def update_rows(self, rows):
        if list(self.rows.values()) != rows:
            self.rows = {str(r["id"]): r for r in rows}
            sync_rows(self.tree, {k: ((title(r), r.get("cwd") or "—"), ())
                                  for k, r in self.rows.items()})
        note = f"{len(rows)} 个会话" if rows else "没有匹配的会话。可清空搜索或检查数据库路径。"
        self.note.configure(text=note, style="Muted.TLabel")

    def select(self, key):
        if self.tree.exists(str(key)):
            self.tree.selection_set(str(key))
            self.tree.focus(str(key))
            self.tree.see(str(key))
        else:
            self.tree.selection_remove(*self.tree.selection())

    def details(self):
        row = self.current() or {}
        for key, field in self.fields.items():
            value = str(row.get(key) or "—")
            if field.get() != value:
                field.set(value)
        self.open.state(["!disabled" if row else "disabled"])


class Panel:
    def __init__(self, root, args, backend):
        self.root, self.args, self.backend = root, args, backend
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.busy = self.dirty = self.dialog = self.ready = self.cleanup = False
        self.timer = self.next_write = None
        self.bindings, self.messages, self.errors = [], {}, {}
        self.restore = True
        self.writing = False
        self.target = None
        root.title("双会话工作台")
        root.geometry("1060x740")
        root.minsize(390, 550)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background="#f5f7f3", foreground="#1d3731", font=("Microsoft YaHei UI", 10))
        style.configure("TNotebook.Tab", padding=(8, 5))
        style.map("TNotebook.Tab", background=[("selected", "#dbe9df")])
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=26)
        style.map("Treeview", background=[("selected", "#d3e5d9")], foreground=[("selected", "#143b30")])
        style.configure("Accent.TButton", background="#245646", foreground="white", padding=(8, 6))
        style.map("Accent.TButton", background=[("active", "#346b57")], foreground=[("disabled", "#bfcfc4")])
        style.configure("Attention.TButton", background="#fff0cf", foreground="#704914", anchor="w")
        style.configure("TNotebook", borderwidth=0)
        style.configure("TNotebook.Tab", borderwidth=0, padding=(10, 6))
        style.configure("Treeview", borderwidth=0)
        style.configure("Treeview.Heading", background="#e9efea", relief="flat", anchor="w")
        style.configure("Rail.TFrame", background="#e7eee7")
        style.configure("Rail.TLabel", background="#e7eee7")
        style.configure("Pair.Treeview", background="#e7eee7", fieldbackground="#e7eee7",
                        font=("Microsoft YaHei UI", 10), rowheight=32, indent=0)
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white", foreground="#193b31")
        style.configure("CardTitle.TLabel", background="white", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Muted.TLabel", foreground="#495865")
        style.configure("Issue.TLabel", foreground="#934412")
        style.configure("Ready.TLabel", foreground="#186143")
        body = ttk.Frame(root, padding=(18, 12))
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="双会话工作台", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="立即刷新", command=self.refresh).pack(side="right")
        self.service = ttk.Label(head, text="正在检查服务…")
        self.service.pack(side="right", padx=12)
        self.service_info = ttk.Label(body, style="Muted.TLabel", wraplength=820)
        self.service_info.pack(anchor="w", pady=(4, 10))
        self.attention_area = ttk.Frame(body)
        self.attention_area.pack(fill="x")
        self.attention = ttk.Button(self.attention_area, style="Attention.TButton", command=self.jump_issue)
        book = ttk.Notebook(body)
        book.pack(fill="both", expand=True)
        pair = ttk.Frame(book, padding=12)
        provision = ttk.Frame(book, padding=12)
        mail = ttk.Frame(book, padding=12)
        self.home = ttk.Frame(book, padding=10)
        book.add(self.home, text="配对首页")
        book.add(pair, text="会话配对")
        book.add(provision, text="  新建配对  ")
        book.add(mail, text="  消息记录  ")
        self.book, self.provision_frame, self.mail = book, provision, mail
        ttk.Label(pair, text="选择已有配对可恢复两侧会话，也可以分别搜索并选择新组合。").pack(anchor="w")
        bar = ttk.Frame(pair)
        bar.pack(fill="x", pady=(8, 10))
        ttk.Label(bar, text="已有配对").pack(side="left", padx=(0, 8))
        self.saved = ttk.Combobox(bar, state="readonly")
        self.saved.pack(side="left", fill="x", expand=True)
        self.saved.bind("<<ComboboxSelected>>", self.choose_binding)
        self.inspect = ttk.Button(bar, text="查看配对", command=self.inspect_binding)
        self.inspect.pack(side="left", padx=(8, 0))
        self.remove = ttk.Button(bar, text="解除配对…", command=self.unbind)
        self.remove.pack(side="left", padx=(8, 0))
        columns = ttk.Frame(pair)
        columns.pack(fill="both", expand=True)
        columns.rowconfigure(0, weight=1)
        self.pickers = {}
        for i, kind in enumerate(KINDS):
            columns.columnconfigure(i, weight=1, uniform="picker")
            picker = Picker(columns, kind, self.changed, self.search,
                            lambda k=kind: self.open_session(k))
            picker.grid(row=0, column=i, sticky="nsew", padx=(0, 5) if i == 0 else (5, 0))
            self.pickers[kind] = picker
        actions = ttk.Frame(pair)
        actions.pack(fill="x", pady=(10, 0))
        self.save = ttk.Button(actions, text="确认并保存配对…", command=self.bind)
        self.save.pack(side="right")
        self.pair_note = ttk.Label(actions, wraplength=545)
        self.pair_note.pack(side="left", fill="x")

        self.provision_direction = tk.StringVar(value="forward")
        direction = ttk.Frame(provision)
        direction.pack(fill="x", pady=(0, 10))
        for value, label in (("forward", "Desktop → Claude"), ("reverse", "Claude → Desktop")):
            ttk.Radiobutton(direction, text=label, value=value, variable=self.provision_direction,
                            command=self.switch_direction).pack(side="left", padx=(0, 12))
        self.forward = ttk.Frame(provision)
        self.reverse = ttk.Frame(provision)
        self.build_reverse()
        self.forward.pack(fill="both", expand=True)
        provision = self.forward
        # 新建配对选项卡UI
        ttk.Label(provision, text="选择已有 Desktop。确认后会自动创建 Claude、初始化并绑定，无需发送首信。", wraplength=320).pack(anchor="w", pady=(0, 12))
        prov_table, self.prov_tree = tree_table(provision, [("title", "Desktop 标题", 240),
                                                             ("native_id", "线程 ID", 280),
                                                             ("cwd", "工作目录", 220)], 10)
        prov_table.pack(fill="both", expand=True, pady=(0, 12))
        self.prov_tree.bind("<<TreeviewSelect>>", lambda _: self._update_provision_form())

        prov_form = ttk.LabelFrame(provision, text="新建 Claude Code 并配对", padding=12)
        prov_form.pack(fill="x")

        # 请求状态显示（独立Label）
        ttk.Label(prov_form, text="请求状态").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.prov_status_var = tk.StringVar(value="")
        ttk.Label(prov_form, textvariable=self.prov_status_var, foreground="#805311").grid(
            row=0, column=1, sticky="w")

        # 会话标题（用户输入）
        ttk.Label(prov_form, text="会话标题").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.prov_title_var = tk.StringVar()
        ttk.Entry(prov_form, textvariable=self.prov_title_var, width=50).grid(row=1, column=1, sticky="ew", pady=(6, 0))

        # 目标目录
        ttk.Label(prov_form, text="目标目录").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.prov_cwd_var = tk.StringVar()
        ttk.Entry(prov_form, textvariable=self.prov_cwd_var, state="readonly", width=50).grid(
            row=2, column=1, sticky="ew", pady=(6, 0))
        prov_form.columnconfigure(1, weight=1)

        prov_actions = ttk.Frame(provision)
        prov_actions.pack(fill="x", pady=(12, 0))
        self.prov_button = ttk.Button(prov_actions, text="确认新建并配对", command=self._create_and_pair)
        self.prov_button.pack(side="right")
        self.prov_button.state(["disabled"])

        self.prov_all_desktops = []
        self.prov_rows = {}
        self.prov_last_selection = None  # 记录上次选中项，避免重复填充
        self.prov_tracking_request = None  # 追踪刚成功提交的请求ID

        self.message_widgets(mail)
        self.build_home()
        self.footer = ttk.Label(body, style="Muted.TLabel", wraplength=910)
        self.footer.pack(fill="x", pady=(7, 0))
        self.changed()
        self.refresh()

    def switch_direction(self):
        frames = {"forward": self.forward, "reverse": self.reverse}
        for frame in frames.values():
            frame.pack_forget()
        frames[self.provision_direction.get()].pack(fill="both", expand=True)

    def build_reverse(self):
        self.reverse_rows, self.drafts = {}, {}
        ttk.Label(self.reverse, text="选择已有 Claude，打开同项目的 Desktop 草稿。请在草稿中原样发送预填首信，核验通过后会自动配对，无需返回确认。", wraplength=320).pack(anchor="w")
        table, self.draft_tree = tree_table(self.reverse,
            [("title", "Claude 会话", 230), ("status", "配对状态", 140), ("cwd", "工作目录", 260)], 6)
        table.pack(fill="both", expand=True, pady=8)
        self.draft_tree.bind("<<TreeviewSelect>>", lambda _: self.draft_selection())
        self.draft_status = ttk.Label(self.reverse, wraplength=320, justify="left")
        self.draft_status.pack(fill="x", pady=(0, 8))
        actions = ttk.Frame(self.reverse)
        actions.pack(fill="x")
        self.draft_create = ttk.Button(actions, text="打开 Desktop 草稿", style="Accent.TButton",
                                      command=lambda: self.draft_action("create"))
        self.draft_create.pack(side="left")
        self.draft_cancel = ttk.Button(actions, text="取消申请…", command=lambda: self.draft_action("cancel"))
        self.draft_cancel.pack(side="left", padx=8)
        self.draft_selection()

    def reverse_state(self, cc):
        binding = next((b for b in self.bindings if b["cc"] == cc["id"] or
                        b["native_cc"] == cc["native_id"]), None)
        if binding:
            return "bound", f"已绑定 · 配对 {binding['id']}"
        req = self.drafts.get(cc["id"], {})
        state = req.get("state")
        if state == "bound":
            return "retired", "此前配对已解除"
        labels = {"pending": "待发送首信", "cancelled": "已取消", "failed": "打开或核验失败"}
        return state, labels.get(state, "未配对")

    def refresh_reverse(self):
        rows = {k: ((title(cc), self.reverse_state(cc)[1], cc["cwd"]), ())
                for k, cc in self.reverse_rows.items()}
        sync_rows(self.draft_tree, rows)
        self.draft_selection()

    def selected_cc(self):
        selected = self.draft_tree.selection()
        return self.reverse_rows.get(selected[0]) if selected else None

    def draft_selection(self):
        cc = self.selected_cc()
        state, label = self.reverse_state(cc) if cc else (None, "请选择已有原生 ID 的 Claude 会话。")
        req = self.drafts.get(cc["id"], {}) if cc else {}
        if state == "pending":
            label += " · 请在 Desktop 草稿原样发送预填首信。核验通过后自动绑定，无需回来确认。草稿已关闭时，可取消后重新打开。"
        elif state == "failed":
            label += " · " + (req.get("detail") or "请检查后重试。")
        elif cc and state != "bound":
            label += " · 打开草稿后，由你发送首信完成配对。"
        self.draft_status.configure(text=label)
        allowed = cc and self.ready and not any(k in self.errors for k in ("cc","bindings","requests"))
        self.draft_create.state(["!disabled" if allowed and state not in ("pending","bound") else "disabled"])
        self.draft_cancel.state(["!disabled" if allowed and state == "pending" else "disabled"])

    def draft_action(self, action):
        cc = self.selected_cc()
        if not cc or not self.ready:
            return
        req_id = self.drafts.get(cc["id"], {}).get("id") if action == "cancel" else None
        heading = "取消 Desktop 草稿申请" if action == "cancel" else "打开 Desktop 草稿"
        explanation = ("取消后，旧草稿的预填首信将失效。" if action == "cancel" else
                       "Desktop 将打开此项目的新草稿。请原样发送预填首信，工作台核验后自动绑定。")
        prompt = f"{title(cc)}\n目录　{cc['cwd']}\nClaude 原生 ID　{cc['native_id']}\n\n{explanation}"
        def submit():
            if not worker_status(self.args.status_file)[0]:
                raise ValueError("日常桥未就绪，请刷新后重试")
            return self.backend.provision.desktop_draft(self.args, cc, action, req_id)
        self.confirm(heading, prompt, submit, lambda status: self.draft_status.configure(text=status))

    def build_home(self):
        home = self.home
        self.home_id = self.issue_id = None
        self.narrow = None
        self.side = ttk.Frame(home, style="Rail.TFrame", padding=(7, 8))
        ttk.Label(self.side, text="我的配对", style="Rail.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self.pair_list = ttk.Treeview(self.side, style="Pair.Treeview", show="tree", selectmode="browse", height=6)
        self.pair_list.column("#0", width=190, minwidth=80)
        scroll = ttk.Scrollbar(self.side, command=self.pair_list.yview)
        self.pair_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.pair_list.pack(fill="both", expand=True)
        self.pair_list.bind("<<TreeviewSelect>>", self.pick_home)
        self.workspace = ttk.Frame(home)
        self.home_title = ttk.Label(self.workspace, style="Title.TLabel")
        self.home_title.pack(anchor="w")
        self.home_dir = ttk.Label(self.workspace, style="Muted.TLabel")
        self.home_dir.pack(anchor="w", pady=(0, 10))
        cards = ttk.Frame(self.workspace)
        cards.pack(fill="x")
        self.card_labels, self.card_buttons = {}, {}
        for col, (kind, who) in enumerate((("desktop", "Astra"), ("cc", "Claude"))):
            cards.columnconfigure(col, weight=1, uniform="card")
            card = ttk.Frame(cards, padding=10, style="Card.TFrame")
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 5) if not col else (5, 0))
            ttk.Label(card, text=NAMES[kind], style="Card.TLabel").pack(anchor="w")
            ttk.Label(card, text=who, style="CardTitle.TLabel").pack(anchor="w")
            self.card_labels[kind] = ttk.Label(card, style="Card.TLabel")
            self.card_labels[kind].pack(fill="x", pady=(3, 8))
            btn = ttk.Button(card, text="打开 Desktop" if kind == "desktop" else "打开 Claude",
                             command=lambda k=kind: self.open_home(k), style="Accent.TButton")
            btn.pack(anchor="w")
            self.card_buttons[kind] = btn
            if kind == "desktop":
                self.hook_button = ttk.Button(card, text="设置项目收件", command=self.setup_home_hooks)
                self.hook_button.pack(anchor="w", pady=(5, 0))
        heading = ttk.Frame(self.workspace)
        heading.pack(fill="x", pady=(16, 5))
        ttk.Label(heading, text="最近往来", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Button(heading, text="查看配对", command=self.inspect_home).pack(side="right")
        frame, self.recent = tree_table(self.workspace, [("state","状态",85),("subject","消息",250)],4)
        frame.pack(fill="both", expand=True)
        self.recent.column("state", width=92, stretch=False)
        self.recent.tag_configure("issue", foreground="#86490b", background="#fff0cf")
        self.recent.bind("<Double-1>", lambda _: self.open_recent())
        self.recent.bind("<Return>", lambda _: self.open_recent())
        self.read_button = ttk.Button(self.workspace, text="查看所选消息", command=self.open_recent)
        self.read_button.pack(anchor="e", pady=(5,0))
        self.recent.bind("<<TreeviewSelect>>", lambda _: self.read_button.state(
            ["!disabled" if self.recent.selection() else "disabled"]))
        home.bind("<Configure>", self.home_layout)

    def home_layout(self, event):
        narrow = event.width < 680
        available = max(120, event.width - (40 if narrow else 256))
        self.service_info.configure(wraplength=max(260, event.width-10))
        self.home_dir.configure(wraplength=available)
        self.home_title.configure(wraplength=available)
        for label in self.card_labels.values():
            label.configure(wraplength=max(105, available // 2 - 30))
        if narrow == self.narrow:
            return
        self.narrow = narrow
        self.side.grid_forget()
        self.workspace.grid_forget()
        self.home.rowconfigure(0, weight=0 if narrow else 1)
        self.home.rowconfigure(1, weight=1 if narrow else 0)
        self.home.columnconfigure(0, weight=1 if narrow else 0)
        self.home.columnconfigure(1, weight=0 if narrow else 1)
        self.side.grid(row=0, column=0, sticky="nsew", padx=(0, 0 if narrow else 16))
        self.pair_list.configure(height=2 if narrow else 6)
        self.workspace.grid(row=int(narrow), column=int(not narrow), sticky="nsew", pady=(8 if narrow else 0, 0))

    def pick_home(self, _):
        sel = self.pair_list.selection()
        if sel and self.home_id != int(sel[0]):
            self.home_id = int(sel[0])
            self.refresh_home()

    @staticmethod
    def short_title(value):
        line = " ".join(value.split())
        return line if len(line) <= 36 else line[:35] + "…"

    def home_binding(self):
        return next((b for b in self.bindings if b["id"] == self.home_id), None)

    def refresh_home(self):
        binding = self.home_binding() or (self.bindings[-1] if self.bindings else None)
        self.home_id = binding["id"] if binding else None
        ids = {str(b["id"]) for b in self.bindings}
        for key in self.pair_list.get_children():
            if key not in ids:
                self.pair_list.delete(key)
        for b in reversed(self.bindings):
            key = str(b["id"])
            caption = b.get("desktop_title") or b["desktop"]
            if self.pair_list.exists(key):
                self.pair_list.item(key, text=self.short_title(caption))
            else:
                self.pair_list.insert("", "end", iid=key, text=self.short_title(caption))
        if binding and self.pair_list.selection() != (str(self.home_id),):
            self.pair_list.selection_set(str(self.home_id))
            self.pair_list.see(str(self.home_id))
        title_text = (binding.get("desktop_title") or "当前配对") if binding else "尚无配对"
        self.home_title.configure(text=self.short_title(title_text))
        self.home_dir.configure(text=(binding.get("desktop_cwd") or "") if binding else "从「新建配对」开始，或在「会话配对」选择两个已有会话。")
        for kind in KINDS:
            self.card_labels[kind].configure(text=self.short_title(binding.get(kind+"_title") or "未命名会话") if binding else "—")
            self.card_buttons[kind].state(["!disabled" if binding and "bindings" not in self.errors else "disabled"])
        self.hook_button.state(["!disabled" if binding and "bindings" not in self.errors else "disabled"])
        messages = list(reversed(self.messages.values()))
        issues = [m for m in messages if display_state(m) in ISSUES]
        self.issue_id = issues[0]["id"] if issues else None
        if issues and "messages" not in self.errors:
            self.attention.configure(text=f"查看待核对消息（{len(issues)}） · {issues[0].get('desktop_title') or '配对'}  →")
            self.attention.pack(fill="x", pady=(0,6))
        else:
            self.attention.pack_forget()
        rows = {str(m["id"]): ((receipt_label(m), ("Claude → Astra" if m["source"] == "cc" else "Astra → Claude") + "　" + (m["body"] or "—").splitlines()[0]),
                ("issue",) if display_state(m) in ISSUES else ()) for m in messages if m["binding_id"] == self.home_id}
        sync_rows(self.recent, rows)
        self.read_button.state(["!disabled" if self.recent.selection() else "disabled"])

    def inspect_home(self):
        binding = self.home_binding()
        if binding:
            messagebox.showinfo("查看配对", describe(*endpoints(binding)), parent=self.root)

    def setup_home_hooks(self):
        binding = self.home_binding()
        if binding:
            project = binding["desktop_cwd"]
            self.confirm("设置项目收件",
                f"Desktop 工作目录\n{project}\n\n启用工作台的 PreToolUse 和 PostToolUse 收件命令。首次设置会合并到 .codex/hooks.json，"
                "需要审核时会打开该目录的 Codex CLI。",
                lambda: self.backend.setup_hooks(binding),
                lambda result: messagebox.showinfo("项目收件", result, parent=self.root))

    def open_home(self, kind):
        binding = self.home_binding()
        if binding:
            self.launch_endpoint(kind, endpoints(binding)[KINDS.index(kind)])

    def open_recent(self):
        sel = self.recent.selection()
        if sel:
            self.show_message(sel[0])

    def jump_issue(self):
        if self.issue_id:
            self.show_message(self.issue_id)

    def show_message(self, key):
        if self.inbox.exists(key):
            self.home_id = self.messages[key]["binding_id"]
            self.refresh_home()
            self.book.select(self.mail)
            self.inbox.selection_set(key)
            self.inbox.see(key)
            self.inbox.focus(key)
            self.message_detail()
            self.detail.focus_set()

    def message_widgets(self, mail):
        self.mail_context = ttk.Label(mail, style="Title.TLabel", wraplength=320)
        self.mail_context.pack(anchor="w", pady=(0, 4))
        top = ttk.Frame(mail)
        top.pack(fill="x")
        self.mail_summary = ttk.Label(top, text="正在读取消息…")
        self.mail_summary.pack(side="left")
        self.resolve_button = ttk.Button(top, text="标记为已处理…", command=self.resolve)
        self.resolve_button.pack(side="right")
        ttk.Button(top,text="按编号查找…",command=self.find_receipt).pack(side="right",padx=4)
        table, self.inbox = tree_table(mail, [("state", "发件回执", 115), ("reply_mode", "回复模式", 75),
                                              ("created", "创建时间", 140), ("body", "消息", 260),
                                              ("detail", "处理详情", 220)], 7)
        table.pack(fill="both", expand=True, pady=(8, 7))
        self.inbox.tag_configure("issue", foreground="#9b341b")
        self.inbox.tag_configure("pending", foreground="#805311")
        ttk.Label(mail, text="所选消息详情").pack(anchor="w")
        frame = ttk.Frame(mail)
        frame.pack(fill="both", expand=True, pady=(4, 0))
        self.detail = tk.Text(frame, height=6, wrap="word", padx=9, pady=7, state="disabled",
                              font="TkDefaultFont", relief="solid", borderwidth=1)
        bar = ttk.Scrollbar(frame, command=self.detail.yview)
        self.detail.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.detail.pack(fill="both", expand=True)
        self.detail_text = None
        self.inbox.bind("<<TreeviewSelect>>", lambda _: self.message_detail())

    def find_receipt(self):
        key=simpledialog.askstring("查回执","信件 ID 或错误编号",parent=self.root)
        if not key:
            return
        key=key.strip().lower()
        if not self.inbox.exists(key):
            messagebox.showinfo("查回执","当前记录中没有这个编号。",parent=self.root)
            return
        self.inbox.selection_set(key)
        self.inbox.focus(key)
        self.inbox.see(key)
        self.message_detail()

    def selected_binding(self):
        index = self.saved.current()
        return self.bindings[index] if 0 <= index < len(self.bindings) else None

    def choose_binding(self, _):
        binding = self.selected_binding()
        if binding:
            for picker in self.pickers.values():
                picker.query.set("")
            self.target = binding["id"]
            self.refresh()

    def search(self):
        self.restore = False
        self.target = None
        self.schedule(220)

    def schedule(self, delay):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.timer = self.root.after(delay, self.refresh)

    def run(self, operation, callback):
        self.busy = True
        future = self.pool.submit(operation)

        def poll():
            if not future.done() or self.dialog:
                self.root.after(40, poll)
                return
            self.busy = False
            if self.next_write is not None:
                operation, after_write = self.next_write
                self.next_write = None
                self.run(operation, after_write)
                return
            self.writing = False
            try:
                result = future.result()
            except Exception as exc:
                self.footer.configure(text=f"操作失败 · {exc}", style="Issue.TLabel")
                messagebox.showerror("操作失败", f"{exc}\n\n如配对含未决消息，请在「消息记录」中核对处理结果。",
                                     parent=self.root)
            else:
                callback(result)
            self.schedule(0 if self.dirty else 3000)

        self.root.after(40, poll)

    def refresh(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.busy or self.dialog:
            self.dirty = True
            return
        self.dirty = False
        queries = tuple(p.query.get() for p in self.pickers.values())

        def apply(result):
            if queries == tuple(p.query.get() for p in self.pickers.values()):
                self.render(*result)
            else:
                self.dirty = True

        self.run(lambda: self.backend.snapshot(queries), apply)

    def render(self, data, errors):
        # 先赋值_all_requests，后续_update_provision_form会用到
        if "requests" in data:
            self._all_requests = data["requests"]
        else:
            self._all_requests = []

        previous = self.selected_binding()
        self.errors = errors
        self.ready, label, info = data["worker"]
        self.service.configure(text=label, style="Ready.TLabel" if self.ready else "Issue.TLabel")
        self.service_info.configure(text=info if not self.ready else info.partition("  ·  PID")[0])
        for kind, picker in self.pickers.items():
            if kind not in errors:
                picker.update_rows(data[kind])
            else:
                picker.note.configure(text=f"读取失败 · {errors[kind]}", style="Issue.TLabel")
        if "bindings" in data:
            self.bindings = data["bindings"]
            choices = [f"{b.get('cc_title') or b['cc']}  ↔  {b.get('desktop_title') or b['desktop']}  "
                       f"〔{b['id']}〕" for b in self.bindings]
            self.saved.configure(values=choices)
            wanted = self.target if self.target is not None else (previous or {}).get("id")
            if self.restore and self.bindings and wanted is None:
                newest = self.bindings[-1]
                wanted = self.target = newest["id"]
            match = next((i for i, b in enumerate(self.bindings) if b["id"] == wanted), None)
            if match is None:
                self.saved.set("请选择已有配对" if choices else "尚无已保存的配对")
                self.target = None
            else:
                self.saved.current(match)
                if self.target is not None and not any(k in errors for k in KINDS):
                    binding = self.bindings[match]
                    self.pickers["cc"].select(binding["cc"])
                    desktop = self.pickers["desktop"]
                    key = next((key for key, row in desktop.rows.items()
                                if native_key(row["native_id"]) == binding["desktop"]), binding["desktop"])
                    desktop.select(key)
                    self.target = None
            self.restore = False
        self.cleanup = data.get("cleanup", False)

        # 更新新建配对选项卡数据
        if "desktop" in data and "bindings" in data:
            paired_ids = {b["desktop"] for b in self.bindings}
            unpaired = [d for d in data.get("desktop_all", data["desktop"])
                       if native_key(d["native_id"]) not in paired_ids]
            self.prov_all_desktops = unpaired
            self.prov_rows = {d["native_id"]: d for d in unpaired}
            prov_rows_data = {d["native_id"]: ((title(d), d["native_id"], d.get("cwd") or "—"), ())
                             for d in unpaired}
            sync_rows(self.prov_tree, prov_rows_data)
            self._update_provision_form()

            # 仅当刚提交的请求变为bound时才自动定位
            if self.prov_tracking_request:
                for req in self._all_requests:
                    if req["id"] == self.prov_tracking_request and req["state"] == "bound":
                        # 找到对应配对，自动选中两个picker并切到配对页
                        if req.get("binding_id"):
                            for i, b in enumerate(self.bindings):
                                if b["id"] == req["binding_id"]:
                                    self.saved.current(i)
                                    # 同步选中两个picker（复用render中已有的逻辑）
                                    self.pickers["cc"].select(b["cc"])
                                    desktop_picker = self.pickers["desktop"]
                                    key = next((key for key, row in desktop_picker.rows.items()
                                               if native_key(row["native_id"]) == b["desktop"]), b["desktop"])
                                    desktop_picker.select(key)
                                    self.home_id = req["binding_id"]
                                    self.book.select(self.home)
                                    break
                        self.prov_tracking_request = None  # 清掉追踪
                        break
        if "messages" in data:
            self.messages = {str(m["id"]): m for m in data["messages"]}
            rows = {}
            pending = issues = 0
            for key, m in reversed(self.messages.items()):
                state = display_state(m)
                pending += state in PENDING
                issues += state in ISSUES
                reply_mode_label = REPLY_MODES.get(m.get("reply_mode", "required"), "—")
                values = (receipt_label(m), reply_mode_label, display_time(m.get("created")),
                          str(m.get("body") or "—").partition("\n")[0],
                          str(m.get("detail") or "").partition("\n")[0])
                tags = ("issue",) if state in ISSUES else ("pending",) if state in PENDING else ()
                rows[key] = values, tags
            sync_rows(self.inbox, rows)
            self.mail_summary.configure(text=f"共 {len(rows)} 条消息　·　处理中 {pending}　·　待核对 {issues}")
            self.book.tab(self.mail, text="消息记录")
        else:
            self.mail_summary.configure(text="消息记录读取失败，请查看下方错误。")
            self.book.tab(self.mail, text="  消息记录 · 读取失败  ")
        labels = {**NAMES, "bindings": "配对", "messages": "消息"}
        status = "　".join(f"{labels[k]}读取失败 · {v}" for k, v in errors.items())
        self.footer.configure(text=status or f"自动刷新中　·　最近更新 {time.strftime('%H:%M:%S')}",
                              style="Issue.TLabel" if errors else "Muted.TLabel")
        if "cc_all" in data:
            self.reverse_rows = {r["id"]: r for r in data["cc_all"]}
        if "drafts" in data:
            self.drafts = {r["cc"]: r for r in data["drafts"]}
        self.refresh_reverse()
        self.refresh_home()
        self.changed()

    def changed(self):
        for picker in self.pickers.values():
            picker.details()
        cc, desktop = (self.pickers[k].current() for k in KINDS)
        paired = cc and desktop and any(
            b["cc"] == cc["id"] and b["native_cc"] == native_key(cc["native_id"])
            and b["desktop"] == native_key(desktop["native_id"]) for b in self.bindings)
        valid = cc and desktop and not paired and not any(k in self.errors for k in (*KINDS, "bindings"))
        self.save.state(["!disabled" if valid else "disabled"])
        self.inspect.state(["!disabled" if self.selected_binding() else "disabled"])
        self.remove.state(["!disabled" if self.selected_binding() and "bindings" not in self.errors
                           else "disabled"])
        self.pair_note.configure(text="两侧会话已配对。" if paired else
                                 "已选好两侧会话，请核对 ID 后保存。" if cc and desktop else
                                 "请在左右列表中各选一个已有会话。")
        self.message_detail()

    def message_detail(self):
        sel = self.inbox.selection()
        m = self.messages.get(sel[0]) if sel else None
        text = "选择一条消息，查看正文、回复和处理详情。"
        if m:
            state = display_state(m)
            src, dst = ("cc", "desktop") if m.get("source") == "cc" else ("desktop", "cc")
            reply_mode = m.get("reply_mode", "required")
            reply_mode_label = REPLY_MODES.get(reply_mode, reply_mode)

            state_note = ""
            if state == "notified":
                state_note = "（单向通知，无需回复）"
            elif state == "awaiting_reply":
                state_note = "（等待后续答复，不阻塞新消息）"

            id_label='错误编号' if m.get('receipt')=='not_sent' else '信件 ID'
            text = (f"{m.get('body') or '—'}\n\n发件回执　{receipt_label(m)}\n依据　{m.get('receipt_evidence') or '—'}\n处理详情　{m.get('detail') or '—'}\n回复　{m.get('reply') or '—'}\n\n{id_label}　{m['id']}　　处理状态　{STATES.get(state, state)}（{state}）{state_note}\n"
                    f"下一步　{m.get('next_action') or '查看处理详情'}\n回复模式　{reply_mode_label}\n"
                    f"配对 {m.get('binding_id', '')}　{src.upper()} → {dst.upper()}\n"
                    f"发件　{m.get(src+'_title', '')}　{m.get(src, '')}\n"
                    f"收件　{m.get(dst+'_title', '')}　{m.get(dst, '')}\n"
                    f"创建时间　{display_time(m.get('created'))}")
        self.mail_context.configure(text=("配对 " + str(m["binding_id"]) + " · " +
            self.short_title(m.get("desktop_title") or m["desktop"])) if m else "消息记录")
        if text != self.detail_text:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", text)
            self.detail.configure(state="disabled")
            self.detail.yview_moveto(0)
            self.detail_text = text
        self.resolve_button.state(["!disabled" if self.cleanup and m and display_state(m) in RECONCILABLE
                                   else "disabled"])

    def confirm(self, heading, prompt, operation, on_success=None, *, needs_note=False):
        if self.dialog or self.writing:
            return
        self.dialog = True
        try:
            answer = confirmation(self.root, heading, prompt, needs_note)
        finally:
            self.dialog = False
        if answer is not None:
            self.dirty = self.writing = True
            action = (lambda: operation(answer)) if needs_note else operation
            work = action, on_success or (lambda _: None)
            if self.busy:
                self.next_write = work
            else:
                self.run(*work)
        else:
            self.refresh()

    def bind(self):
        cc, desktop = (self.pickers[k].current() for k in KINDS)
        expected = conflicts(self.bindings, cc, desktop)
        replaced = "\n\n".join(f"配对 {b['id']}\n{describe(*endpoints(b))}" for b in expected.values())
        prompt = ("保存即授权这两个会话双向发送定向消息。在 CC 中先说一次“问 Astra：你好”。"
                  "桥会在 Desktop 会话中发送一次介绍。\n\n"
                  f"新配对\n{describe(cc, desktop)}\n\n将解除的已有配对：\n{replaced or '无'}")

        def saved(binding_id):
            self.target = binding_id

        self.confirm("确认双向配对", prompt, lambda: self.backend.bind(cc, desktop, expected), saved)

    def inspect_binding(self):
        binding = self.selected_binding()
        messagebox.showinfo(f"已保存的配对 {binding['id']}", describe(*endpoints(binding)), parent=self.root)

    def unbind(self):
        binding = self.selected_binding()
        prompt = (f"解除配对 {binding['id']}？\n\n{describe(*endpoints(binding))}"
                  "\n\n解除后，这组会话的双向配对授权将终止。")
        self.confirm("解除已有配对", prompt, lambda: self.backend.unbind(binding), self.offer_disable_hooks)

    def offer_disable_hooks(self, project):
        if project is not None:
            self.confirm("停用本目录收件",
                f"{project}\n\n该目录的最后一组配对已解除。停用工作台的两条收件命令后，Codex 会保留对它们的信任。"
                "选择「取消」可继续启用收件。",
                lambda: self.backend.disable_unused_hooks(project),
                lambda _: messagebox.showinfo("项目收件", f"{project}\n\n两条工作台收件命令已停用，信任已保留。",
                                               parent=self.root))

    def _update_provision_form(self):
        """更新新建配对表单 - 仅在选项改变时更新标题和路径"""
        sel = self.prov_tree.selection()
        current_selection = sel[0] if sel else None

        # 检查选中项是否改变
        if current_selection != self.prov_last_selection:
            self.prov_last_selection = current_selection
            if not current_selection:
                self.prov_title_var.set("")
                self.prov_cwd_var.set("")
                self.prov_button.state(["disabled"])
                return

            desktop = self.prov_rows.get(current_selection)
            if desktop:
                # 仅在选项改变时填充标题和路径默认值
                self.prov_title_var.set(title(desktop))
                self.prov_cwd_var.set(desktop.get("cwd") or "—")

        # 状态和按钮每次都更新（从_all_requests）
        desktop = self.prov_rows.get(current_selection) if current_selection else None
        if not desktop:
            self.prov_button.state(["disabled"])
            return

        request = self._find_request_for_desktop(current_selection)

        if request:
            state = request["state"]
            status_map = {
                "queued": "排队中",
                "creating": "正在创建",
                "starting": "初始化中",
                "unknown": "结果待核对",
                "failed": "失败",
                "bound": "已完成"
            }
            status_label = status_map.get(state, state)
            request_id = request["id"]  # 显示完整ID，不截断
            detail = request.get("detail", "")
            status_text = f"{status_label}  请求 {request_id}"
            if detail:
                status_text += f"  {detail[:40]}"
            self.prov_status_var.set(status_text)

            # 控制按钮状态
            if state in ("queued", "creating", "starting", "unknown"):
                self.prov_button.state(["disabled"])
            elif state == "failed":
                self.prov_button.state(["!disabled"])
            else:
                self.prov_button.state(["disabled"])
        else:
            self.prov_status_var.set("")
            self.prov_button.state(["!disabled"])

    def _find_request_for_desktop(self, desktop_native_id):
        """查找Desktop的最新请求"""
        requests = getattr(self, "_all_requests", [])
        for req in reversed(requests):
            if req["desktop"] == desktop_native_id:
                return req
        return None

    def _create_and_pair(self):
        """新建Claude并配对 - 使用后台工作流"""
        sel = self.prov_tree.selection()
        if not sel:
            return
        desktop = self.prov_rows.get(sel[0])
        if not desktop:
            return
        session_title = self.prov_title_var.get().strip()
        if not session_title:
            messagebox.showwarning("输入错误", "请填写会话标题。")
            return

        prompt = (f"将为以下 Desktop 线程新建 Claude Code 会话并配对：\n\n"
                  f"Desktop: {title(desktop)}\n"
                  f"线程 ID: {desktop['native_id']}\n"
                  f"目录: {desktop.get('cwd') or '—'}\n\n"
                  f"新会话标题: {session_title}\n\n"
                  "确认后将提交创建请求。")

        def do_create():
            return self.backend.create_request(desktop["native_id"], session_title)

        def on_created(result):
            # 立即设置tracking id
            self.prov_tracking_request = result["id"]
            request_id = result["id"]
            state = result["state"]
            detail = f"请求已创建，ID: {request_id}，状态: {state}"
            messagebox.showinfo("已提交", detail)

        # 只用一次self.confirm，不再前置confirmation
        self.confirm("确认新建并配对", prompt, do_create, on_created)

    def resolve(self):
        row = self.messages[self.inbox.selection()[0]]
        prompt = (f"消息 ID　{row['id']}\n当前状态　{STATES.get(row['state'], row['state'])}\n"
                  f"处理详情　{row.get('detail') or '—'}\n\n"
                  "请先在原生会话核对实际处理结果；若工作进程卡住，请先停止该进程。"
                  "确认后将关闭此条未决消息并保存核对说明。此操作不会重新投递。")
        self.confirm("关闭未决消息", prompt, lambda note: self.backend.store.reconcile(
            row["id"], row["state"], note), needs_note=True)

    def open_session(self, kind):
        self.launch_endpoint(kind, self.pickers[kind].current())

    def launch_endpoint(self, kind, row):
        if not row:
            return
        sid = quote(str(row["native_id"] if kind == "desktop" else row["id"]), safe="")
        try:
            if kind == "desktop":
                os.startfile(f"codex://threads/{sid}")
            else:
                if self.args.status_file and not self.ready:
                    messagebox.showinfo("请先启动服务", OFFLINE, parent=self.root)
                    return
                if not webbrowser.open(self.args.cc_url + "/session/" + sid):
                    raise OSError("未能启动默认浏览器。")
        except (OSError, webbrowser.Error) as exc:
            messagebox.showerror("打开会话失败", f"请检查默认浏览器或 Codex Desktop 的链接关联。\n{exc}",
                                 parent=self.root)

    def close(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Win1 原生会话配对面板")
    for name in ("cc-db", "desktop-db", "store"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cc-url", required=True, type=loopback_url)
    parser.add_argument("--status-file", help="带桥服务的 JSON 心跳文件")
    args = parser.parse_args()
    if __package__:
        from .catalog import list_sessions
        from .storage import Store
        from . import provision
    else:
        from catalog import list_sessions
        from storage import Store
        import provision
    root = tk.Tk()
    Panel(root, args, Backend(args, list_sessions, Store, provision))
    root.mainloop()


if __name__ == "__main__":
    main()
