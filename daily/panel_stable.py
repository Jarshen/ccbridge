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
from tkinter import messagebox, ttk
from urllib.parse import quote, urlsplit
import webbrowser
from uuid import UUID


OFFLINE = "可先保存配对。请从日常启动入口启动带桥服务。启动前请先关闭普通 CloudCLI 服务，待桥服务就绪后再试用。"
KINDS = ("cc", "desktop")
NAMES = {"cc": "Claude Code", "desktop": "Codex Desktop"}
STATES = {
    "pending": "待处理", "queued": "排队中", "submitting": "提交中",
    "submitted": "已提交", "received": "已接收", "running": "处理中",
    "returning": "回传中", "returned": "已回传", "delivered": "已送达",
    "completed": "已完成", "done": "已完成", "replied": "已回复",
    "failed": "失败", "error": "错误", "unknown": "结果待核对",
    "unresolved": "待人工处理", "resolved": "已人工处理", "cancelled": "已取消",
}
PENDING = {"pending", "queued", "submitting", "submitted", "received", "running", "returning"}
ISSUES = {"failed", "error", "unknown", "unresolved"}
RECONCILABLE = {"submitting", "submitted", "received", "returning", "failed", "unknown"}


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

    def __init__(self, args, catalog, store_type):
        self.args, self.catalog, self.store_type = args, catalog, store_type
        self.store = None

    def bindings(self):
        return [b for b in self.store.bindings() if b["active"]]

    def snapshot(self, queries):
        result, errors = {}, {}
        for kind, query in zip(KINDS, queries):
            try:
                result[kind] = self.catalog(kind, getattr(self.args, kind + "_db"), query)
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
                result["messages"] = self.store.messages()
                result["cleanup"] = callable(getattr(self.store, "reconcile", None))
            else:
                errors["messages"] = errors["bindings"]
        except Exception as exc:
            errors["messages"] = str(exc)
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
        root.title("Win1 · 原生会话配对")
        root.geometry("1060x740")
        root.minsize(800, 610)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Muted.TLabel", foreground="#495865")
        style.configure("Issue.TLabel", foreground="#934412")
        style.configure("Ready.TLabel", foreground="#186143")
        body = ttk.Frame(root, padding=(18, 12))
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="原生会话配对", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="立即刷新", command=self.refresh).pack(side="right")
        self.service = ttk.Label(head, text="正在检查服务…")
        self.service.pack(side="right", padx=12)
        self.service_info = ttk.Label(body, style="Muted.TLabel", wraplength=820)
        self.service_info.pack(anchor="w", pady=(4, 10))
        book = ttk.Notebook(body)
        book.pack(fill="both", expand=True)
        pair = ttk.Frame(book, padding=12)
        mail = ttk.Frame(book, padding=12)
        book.add(pair, text="  会话配对  ")
        book.add(mail, text="  消息记录  ")
        self.book, self.mail = book, mail
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
        self.message_widgets(mail)
        self.footer = ttk.Label(body, style="Muted.TLabel", wraplength=910)
        self.footer.pack(fill="x", pady=(7, 0))
        self.changed()
        self.refresh()

    def message_widgets(self, mail):
        top = ttk.Frame(mail)
        top.pack(fill="x")
        self.mail_summary = ttk.Label(top, text="正在读取消息…")
        self.mail_summary.pack(side="left")
        self.resolve_button = ttk.Button(top, text="标记为已处理…", command=self.resolve)
        self.resolve_button.pack(side="right")
        table, self.inbox = tree_table(mail, [("state", "状态", 105), ("created", "创建时间", 150),
                                              ("body", "消息", 285), ("detail", "处理详情", 235)], 7)
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
        previous = self.selected_binding()
        self.errors = errors
        self.ready, label, info = data["worker"]
        self.service.configure(text=label, style="Ready.TLabel" if self.ready else "Issue.TLabel")
        self.service_info.configure(text=info)
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
        if "messages" in data:
            self.messages = {str(m["id"]): m for m in data["messages"]}
            rows = {}
            pending = issues = 0
            for key, m in reversed(self.messages.items()):
                state = m["state"]
                pending += state in PENDING
                issues += state in ISSUES
                values = (STATES.get(state, state), display_time(m.get("created")),
                          str(m.get("body") or "—").partition("\n")[0],
                          str(m.get("detail") or "").partition("\n")[0])
                tags = ("issue",) if state in ISSUES else ("pending",) if state in PENDING else ()
                rows[key] = values, tags
            sync_rows(self.inbox, rows)
            self.mail_summary.configure(text=f"共 {len(rows)} 条消息　·　处理中 {pending}　·　待核对 {issues}")
            self.book.tab(self.mail, text=f"  消息记录　处理中 {pending} / 待核对 {issues}  ")
        else:
            self.mail_summary.configure(text="消息记录读取失败，请查看下方错误。")
            self.book.tab(self.mail, text="  消息记录 · 读取失败  ")
        labels = {**NAMES, "bindings": "配对", "messages": "消息"}
        status = "　".join(f"{labels[k]}读取失败 · {v}" for k, v in errors.items())
        self.footer.configure(text=status or f"自动刷新中　·　最近更新 {time.strftime('%H:%M:%S')}",
                              style="Issue.TLabel" if errors else "Muted.TLabel")
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
            state = m["state"]
            src, dst = ("cc", "desktop") if m.get("source") == "cc" else ("desktop", "cc")
            text = (f"消息 ID　{m['id']}　　状态　{STATES.get(state, state)}（{state}）\n"
                    f"配对 {m.get('binding_id', '')}　{src.upper()} → {dst.upper()}\n"
                    f"发件　{m.get(src+'_title', '')}　{m.get(src, '')}\n"
                    f"收件　{m.get(dst+'_title', '')}　{m.get(dst, '')}\n"
                    f"创建时间　{display_time(m.get('created'))}\n\n正文\n{m.get('body') or '—'}"
                    f"\n\n回复\n{m.get('reply') or '—'}\n\n处理详情\n{m.get('detail') or '—'}")
        if text != self.detail_text:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", text)
            self.detail.configure(state="disabled")
            self.detail_text = text
        self.resolve_button.state(["!disabled" if self.cleanup and m and m["state"] in RECONCILABLE
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
        self.confirm("解除已有配对", prompt, lambda: self.backend.unbind(binding))

    def resolve(self):
        row = self.messages[self.inbox.selection()[0]]
        prompt = (f"消息 ID　{row['id']}\n当前状态　{STATES.get(row['state'], row['state'])}\n"
                  f"处理详情　{row.get('detail') or '—'}\n\n"
                  "请先在原生会话核对实际处理结果；若工作进程卡住，请先停止该进程。"
                  "确认后将关闭此条未决消息并保存核对说明。此操作不会重新投递。")
        self.confirm("关闭未决消息", prompt, lambda note: self.backend.store.reconcile(
            row["id"], row["state"], note), needs_note=True)

    def open_session(self, kind):
        row = self.pickers[kind].current()
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
    else:
        from catalog import list_sessions
        from storage import Store
    root = tk.Tk()
    Panel(root, args, Backend(args, list_sessions, Store))
    root.mainloop()


if __name__ == "__main__":
    main()
