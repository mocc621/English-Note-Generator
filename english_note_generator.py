# -*- coding: utf-8 -*-
"""
英语笔记生成器 —— 桌面版（tkinter）

用法只有一个动作：粘贴英文 → 点「一键生成笔记」。
笔记栏只有两块：翻译（每段英文下面直接接中文，重点词绿色标出）和重点词。

重点词后面带一颗 ☆：点一下 ☆ 就收藏进「生词本」文件夹（变成 ★），
再点一下取消收藏。收藏是长期的，跨次生成都留着，可以导出成
Word / Markdown / Anki CSV 拿去背。

翻译和查词都是自动的，不需要填任何密钥、不需要选任何选项。
需要联网（用公开的免费翻译与词典接口），成功的结果会缓存在本地，下次不再重复请求。

每次生成笔记，都会自动在程序所在文件夹里存一份 Word：
    English_Notes_20250131_142530.docx
底部按钮：一键生成笔记 / 导出 Word / 导出 Markdown / 导出 Anki CSV / 生词本 / 清空。
"""

import datetime
import json
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Dict, List, Optional, Tuple

import dictionary
import notebook_engine as engine
import translator_api as trans
import vocab_book

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

UI_FONT = ("Microsoft YaHei UI", 10)
TEXT_FONT = ("Microsoft YaHei UI", 11)
# 音标必须用含 IPA 长音符 ː (U+02D0) 字形的字体。
# 实测 Consolas / Cascadia Mono / Lucida Console / Cambria 缺这个字形，
# 会把 /əˈsɜːt/ 显示成 /əˈsɜt/，所以音标单独用 Segoe UI。
IPA_FONT = ("Segoe UI", 12)
SERIF_FONT = ("Georgia", 12)
# ☆ / ★ 用的是 Segoe UI Symbol，保证在 Windows 上不是方块
STAR_FONT = ("Segoe UI Symbol", 12)

PLACEHOLDER = (
    "把英文粘贴到这里（Ctrl+V）……\n\n"
    "然后点下面的「一键生成笔记」。\n"
    "空行分段，每段会各自翻译。"
)

# 每轮最多联网查多少个重点词（防止大量请求）
MAX_LOOKUP_WORDS = 30

# 导出的 Word / Markdown / Anki CSV 统一放在程序所在文件夹的这个子文件夹里
NOTES_FOLDER_NAME = "导出的笔记"

# 收藏的重点词单独放在这个子文件夹里（见 vocab_book.py）
VOCAB_FOLDER_NAME = vocab_book.FOLDER_NAME

# 笔记里收藏按钮的两个符号
STAR_OFF = "☆"
STAR_ON = "★"

# 查不到释义时的占位提示（收藏时不能把它当成"释义"存进生词本）
NO_MEANING_HINT = "（还没有释义，可点「查词」看一看）"


def load_config() -> Dict[str, str]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(data: Dict[str, str]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


# Word 里重点词的绿色（与界面上的绿色保持一致）
WORD_GREEN = "1B8A3A"
WORD_GREEN_FILL = "E8F5E9"
# 界面上原文里重点词的绿色（带 # 号）
WORD_GREEN_UI = "#1b8a3a"
WORD_GREEN_FILL_UI = "#e8f5e9"


def add_highlighted_paragraph(doc, text: str, key_words: set):
    """
    往 Word 里写一段英文，并把命中的重点词标绿（绿字 + 浅绿底）。

    做法：把段落按"单词 / 非单词"切分，逐段插入 run，
    只有命中的 run 才上绿色。这样不会破坏原文的空格和标点。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import RGBColor

    paragraph = doc.add_paragraph()
    if not key_words:
        paragraph.add_run(text)
        return paragraph

    for token in re.findall(r"[A-Za-z][A-Za-z'’\-]*|[^A-Za-z]+", text):
        run = paragraph.add_run(token)
        if token[:1].isalpha() and token.lower() in key_words:
            run.font.color.rgb = RGBColor.from_string(WORD_GREEN)
            run.font.bold = True
            # 浅绿底纹（w:fill 必须是十六进制色值）
            shading = OxmlElement("w:shd")
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:color"), "auto")
            shading.set(qn("w:fill"), WORD_GREEN_FILL)
            run._element.get_or_add_rPr().append(shading)
    return paragraph


def make_vocab_entry(word: str, row: Optional[tuple] = None,
                     sentences: Optional[List[str]] = None) -> vocab_book.VocabEntry:
    """
    把"笔记里的一个重点词 + 它的详情行"整理成一条生词本记录。

    row 就是 _key_word_rows() 的一行：
        (单词, 音标, 词性, 释义, 文中形态, 附加说明)
    """
    _, phonetic, pos, meaning, surface, extra = (row or ("", "", "", "", "", ""))
    meaning = str(meaning or "").strip()
    if meaning == NO_MEANING_HINT:
        # 界面上用这句话当占位提示，但它不是真的释义，不该存进生词本
        meaning = ""
    tags = [t for t in re.split(r"[、,，\s]+", str(extra or "")) if t]
    return vocab_book.VocabEntry(
        word=word,
        phonetic=str(phonetic or "").strip(),
        pos=str(pos or "").strip(),
        meaning=meaning,
        sentences=[s for s in (sentences or []) if s],
        note=str(surface or "").strip(),
        tags=tags,
    )


# ============================================================ 生词本窗口


class VocabBookDialog(tk.Toplevel):
    """
    生词本窗口：看收藏、删收藏、导出 Word / Markdown / Anki CSV。

    界面上只有一个列表，双击看详情；收藏本身存在「生词本/生词本.json」，
    所以关掉窗口、甚至重开程序，收藏都还在。
    """

    def __init__(self, master, store: vocab_book.Store, on_change=None):
        super().__init__(master)
        self.title(f"生词本（{len(store)} 个词）")
        self.store = store
        self.on_change = on_change
        self.transient(master)
        self.geometry("860x520")
        self.minsize(680, 360)
        self._build()
        self._fill()

    # ---------------------------------------------------- 界面

    def _build(self) -> None:
        head = ttk.Frame(self, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        ttk.Label(head, text="生词本", style="Title.TLabel").pack(side="left")
        self.summary = ttk.Label(
            head,
            text=f"保存在：{os.path.relpath(self.store.folder, BASE_DIR)}　（双击看详情，选中后按 Delete 删除）",
            style="Hint.TLabel",
        )
        self.summary.pack(side="left", padx=(10, 0))

        body = ttk.Frame(self, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        columns = ("word", "phonetic", "pos", "meaning", "added")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="extended")
        for key, text, width, anchor in (
            ("word", "单词", 150, "w"),
            ("phonetic", "音标", 150, "w"),
            ("pos", "词性", 60, "center"),
            ("meaning", "释义", 300, "w"),
            ("added", "收藏时间", 140, "center"),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "meaning"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self._on_detail)
        self.tree.bind("<Delete>", lambda e: self._on_remove())

        bar = ttk.Frame(self, padding=(12, 8, 12, 6))
        bar.pack(fill="x")
        self.docx_btn = ttk.Button(bar, text="导出 Word", command=self.on_export_docx, width=11)
        self.docx_btn.pack(side="left", ipady=4)
        self.md_btn = ttk.Button(bar, text="导出 Markdown", command=self.on_export_markdown, width=14)
        self.md_btn.pack(side="left", padx=6, ipady=4)
        self.anki_btn = ttk.Button(bar, text="导出 Anki CSV", command=self.on_export_anki, width=14)
        self.anki_btn.pack(side="left", ipady=4)
        ttk.Button(bar, text="打开文件夹", command=self.on_open_folder, width=11).pack(side="left", padx=6, ipady=4)
        ttk.Button(bar, text="删除选中", command=self._on_remove, width=10).pack(side="left", ipady=4)
        ttk.Button(bar, text="关闭", command=self.destroy, width=8).pack(side="right", ipady=4)

        self.status = ttk.Label(self, text="", style="Hint.TLabel", padding=(12, 0, 12, 10))
        self.status.pack(fill="x")

    def _center_on(self, master) -> None:
        """把生词本窗口摆到主窗口中间偏上，别缩在屏幕角落。"""
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + max((master.winfo_height() - self.winfo_height()) // 3, 0)
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:
            pass

    def _fill(self) -> None:
        """按"最近收藏"倒序刷新列表。"""
        for item in self.tree.get_children():
            self.tree.delete(item)
        for entry in self.store.newest_first():
            self.tree.insert(
                "", "end", iid=entry.key,
                values=(entry.word, entry.phonetic or "—", entry.pos or "—",
                        entry.meaning or "—", entry.updated_at or entry.added_at or "—"),
            )
        self.title(f"生词本（{len(self.store)} 个词）")
        if not len(self.store):
            self.status.configure(text="还没有收藏任何词。回主窗口，点重点词后面的 ☆ 就能收藏。")

    # ---------------------------------------------------- 操作

    def _selected_keys(self) -> List[str]:
        return [self.tree.set(iid, "word").lower() for iid in self.tree.selection()]

    def _on_detail(self, event=None) -> None:
        keys = self._selected_keys()
        if not keys:
            return
        entry = self.store.get(keys[0])
        if not entry:
            return
        lines = [entry.word]
        if entry.phonetic:
            lines.append("    " + entry.phonetic)
        if entry.pos or entry.meaning:
            lines.append(f"    {entry.pos} {entry.meaning}".rstrip())
        for sentence in entry.sentences:
            lines.append(f"    原句：{sentence}")
        if entry.note:
            lines.append(f"    {entry.note}")
        meta = []
        if entry.tags:
            meta.append("标签：" + "、".join(entry.tags))
        if entry.added_at:
            meta.append(f"收藏于 {entry.added_at}")
        if meta:
            lines.append("    " + "　".join(meta))
        messagebox.showinfo(f"生词本：{entry.word}", "\n".join(lines), parent=self)

    def _on_remove(self) -> None:
        keys = self._selected_keys()
        if not keys:
            messagebox.showinfo("提示", "先在上面选中要删除的词。", parent=self)
            return
        if not messagebox.askyesno(
            "确认删除", f"从生词本里删除这 {len(keys)} 个词？\n\n" + "、".join(keys), parent=self
        ):
            return
        for key in keys:
            self.store.remove(key)
        self._fill()
        self.status.configure(text=f"已删除 {len(keys)} 个词。")
        if self.on_change:
            self.on_change()

    def on_open_folder(self) -> None:
        if vocab_book.open_folder(self.store.folder):
            self.status.configure(text=f"已打开文件夹：{self.store.folder}")
        else:
            self.status.configure(text=f"没能自动打开，请手动打开：{self.store.folder}")

    def on_export_markdown(self) -> None:
        path = vocab_book.timestamped_path(".md", folder=self.store.folder)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(vocab_book.render_markdown(self.store.entries))
        except OSError as exc:
            messagebox.showerror("保存失败", f"写文件时出错：{exc}", parent=self)
            return
        self.status.configure(text=f"已导出：{os.path.basename(path)}（在「{VOCAB_FOLDER_NAME}」里）")

    def on_export_anki(self) -> None:
        path = vocab_book.timestamped_path("_Anki.csv", folder=self.store.folder)
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(vocab_book.render_anki_csv(self.store.entries))
        except OSError as exc:
            messagebox.showerror("保存失败", f"写文件时出错：{exc}", parent=self)
            return
        self.status.configure(text=f"已导出：{os.path.basename(path)}（在「{VOCAB_FOLDER_NAME}」里）")

    def on_export_docx(self) -> None:
        if not len(self.store):
            messagebox.showinfo("提示", "生词本还是空的，先收藏几个词吧。", parent=self)
            return
        path = vocab_book.timestamped_path(".docx", folder=self.store.folder)
        try:
            self._write_docx(path)
        except ImportError:
            messagebox.showwarning(
                "Word 没能生成", "生成 Word 需要一个库：\n\n    pip install python-docx", parent=self
            )
            return
        except Exception as exc:
            messagebox.showwarning("Word 没能生成", f"生成 Word 时出错：{exc}", parent=self)
            return
        self.status.configure(text=f"已导出：{os.path.basename(path)}（在「{VOCAB_FOLDER_NAME}」里）")

    def _write_docx(self, path: str) -> None:
        from docx import Document

        entries = self.store.entries
        doc = Document()
        doc.add_heading("生词本", level=0)
        doc.add_paragraph(f"共 {len(entries)} 个词（由「英语笔记生成器」导出）")

        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        header = table.rows[0].cells
        for i, name in enumerate(["单词", "音标", "词性", "释义"]):
            header[i].text = name
        for entry in entries:
            cells = table.add_row().cells
            cells[0].text = entry.word
            cells[1].text = entry.phonetic or "—"
            cells[2].text = entry.pos or "—"
            cells[3].text = entry.meaning

        # 例句单独一节：表格里塞长句子不好看
        with_sentences = [e for e in entries if e.sentences]
        if with_sentences:
            doc.add_heading("例句", level=1)
            for entry in with_sentences:
                doc.add_paragraph(entry.word, style="Intense Quote")
                for sentence in entry.sentences:
                    doc.add_paragraph(sentence)
        doc.save(path)


# ============================================================ 设置（只留 AI 总结，可选）


class SettingsDialog(tk.Toplevel):
    """可选的 AI 大意总结配置。普通使用完全不需要打开这个窗口。"""

    def __init__(self, master, cfg: trans.TranslatorConfig):
        super().__init__(master)
        self.title("可选设置")
        self.transient(master)
        self.resizable(False, False)
        self.result: Optional[trans.TranslatorConfig] = None
        self.cfg = cfg

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="带「· 大意」的那条总结能自动生成，不需要任何配置。",
            style="Head.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "如果你想让大意更凝练（由 AI 写而不是按主题句归纳），\n"
                "可以填一个大模型的密钥。不填也完全能用。"
            ),
            foreground="#666666",
            justify="left",
        ).pack(anchor="w", pady=(2, 10))

        form = ttk.Frame(body)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.llm_key_var = tk.StringVar(value=cfg.llm_key)
        self.llm_base_var = tk.StringVar(value=cfg.llm_base)
        self.llm_model_var = tk.StringVar(value=cfg.llm_model)
        preset_names = [name for name, _, _ in trans.LLM_PRESETS]
        self.preset_var = tk.StringVar(value=self._preset_name_for(cfg.llm_base))

        rows = [
            ("服务商", ttk.Combobox(form, textvariable=self.preset_var, values=preset_names,
                                  state="readonly", width=30)),
            ("AI 密钥", ttk.Entry(form, textvariable=self.llm_key_var, width=44, show="•")),
            ("接口地址", ttk.Entry(form, textvariable=self.llm_base_var, width=44)),
            ("模型名", ttk.Entry(form, textvariable=self.llm_model_var, width=44)),
        ]
        for index, (label, widget) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", pady=4, padx=(0, 10))
            widget.grid(row=index, column=1, sticky="ew", pady=4)
        rows[0][1].bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="显示密钥明文", variable=self.show_var, command=self._toggle_show).pack(
            anchor="w", pady=(8, 0)
        )
        ttk.Label(
            body,
            text="密钥明文保存在程序旁边的 config.json 里。",
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=self._on_save).pack(side="right")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=8)

        self._entries = [w for _, w in rows if isinstance(w, ttk.Entry)]
        self.bind("<Escape>", lambda e: self.destroy())
        self._center_on(master)

    @staticmethod
    def _preset_name_for(base: str) -> str:
        for name, url, _ in trans.LLM_PRESETS:
            if url and url == base:
                return name
        return "自定义" if base else trans.LLM_PRESETS[0][0]

    def _apply_preset(self) -> None:
        for name, url, model in trans.LLM_PRESETS:
            if name == self.preset_var.get() and url:
                self.llm_base_var.set(url)
                self.llm_model_var.set(model)
                break

    def _toggle_show(self) -> None:
        char = "" if self.show_var.get() else "•"
        for entry in self._entries:
            entry.configure(show=char)

    def _on_save(self) -> None:
        self.result = trans.TranslatorConfig(
            llm_key=self.llm_key_var.get().strip(),
            llm_base=self.llm_base_var.get().strip(),
            llm_model=self.llm_model_var.get().strip(),
        )
        self.destroy()

    def _center_on(self, master) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + max((master.winfo_height() - self.winfo_height()) // 3, 0)
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:
            pass


# ============================================================ 主窗口


class EnglishNoteApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("英语笔记生成器")
        self.root.geometry("1320x840")
        self.root.minsize(1000, 640)

        self.lexicon = engine.Lexicon(engine.load_vocab())
        self.dict_cache: Dict[str, dict] = dictionary.load_cache()
        self.auto_cache: Dict[str, str] = trans.load_auto_cache()
        self.tm: Dict[str, str] = engine.load_tm()

        self.config_data = load_config()
        self.ai_config = trans.TranslatorConfig(
            llm_key=self.config_data.get("llm_key", ""),
            llm_base=self.config_data.get("llm_base", trans.LLM_PRESETS[0][1]),
            llm_model=self.config_data.get("llm_model", trans.LLM_PRESETS[0][2]),
        )

        self.analysis: Optional[engine.Analysis] = None
        self.entry_lookups: Dict[str, engine.VocabEntry] = {}
        self.online_lookups: Dict[str, dictionary.DictLookup] = {}
        self.ai_summary = ""
        self.busy = False
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self._placeholder_active = False
        self._docx_warned = False          # Word 生成失败的提示只弹一次
        # 重点词的"手工改动"：用户勾选加进来的 / 用户删掉的
        self.added_words: set = set()
        self.removed_words: set = set()
        # 已展开详情的重点词（点一下展开，再点收起）
        self.expanded_words: set = set()
        # 正在重试翻译的段落序号
        self.retrying_paragraphs: set = set()
        # 生词本：收藏的重点词，长期保存在「生词本」文件夹里
        self.vocab_book: vocab_book.Store = vocab_book.Store.load()
        self.vocab_dialog: Optional[VocabBookDialog] = None

        self._build_ui()
        self._show_input_hint()
        self._render_vocab_store()
        self._poll_queue()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-Return>", lambda e: self.on_generate())
        self.root.bind("<Control-d>", lambda e: self.on_lookup_word())

    # ---------------------------------------------------- 界面

    def _build_ui(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure(".", font=UI_FONT)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Head.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

        header = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="英语笔记生成器", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="　　粘贴英文，一键生成笔记", style="Hint.TLabel").pack(side="left")
        ttk.Button(header, text="⚙", width=3, command=self.on_open_settings).pack(side="right")
        ttk.Button(header, text="查词", width=6, command=self.on_lookup_word).pack(side="right", padx=6)
        self.vocab_btn = ttk.Button(header, text="生词本", width=13, command=self.on_open_vocab_book)
        self.vocab_btn.pack(side="right")

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        # ---- 左：输入 ----
        left = ttk.LabelFrame(paned, text="粘贴英文", padding=8)
        paned.add(left, weight=4)
        left.rowconfigure(0, weight=1)      # 原文
        left.columnconfigure(0, weight=1)

        self.input_text = tk.Text(
            left, wrap="word", font=SERIF_FONT, undo=True, padx=8, pady=8,
            relief="flat", highlightthickness=1, highlightbackground="#cccccc",
        )
        self.input_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, command=self.input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.input_text.configure(yscrollcommand=scroll.set)
        self.input_text.tag_configure("plain_hint", foreground="#999999")
        # 右键时临时标绿的标签（如果用户没选择加入，就撤销这个标记）
        self.input_text.tag_configure("pending_add", foreground=WORD_GREEN_UI, background=WORD_GREEN_FILL_UI)
        # 清提示：任何按键 / 粘贴 / 点击都先清掉灰色提示（只清一次，不碰用户内容）
        self.input_text.bind("<Key>", self._clear_placeholder)
        self.input_text.bind("<<Paste>>", lambda e: self.root.after(1, self._clear_placeholder))
        self.input_text.bind("<KeyRelease>", self._on_input_changed)
        self.input_text.bind("<FocusOut>", self._on_input_focus_out)
        # 加入重点词：只保留右键
        self.input_text.bind("<Button-3>", self._on_input_context)
        self._pending_add_index = None      # 当前临时标绿的词的位置

        # ---- 右：笔记（翻译 + 重点词），也是拖拽的投放区 ----
        right = ttk.LabelFrame(paned, text="笔记（翻译 + 重点词）", padding=6)
        paned.add(right, weight=6)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.note_text = tk.Text(
            right, wrap="word", font=TEXT_FONT, padx=14, pady=12,
            relief="flat", highlightthickness=2, highlightbackground="#dddddd",
        )
        self.note_text.grid(row=0, column=0, sticky="nsew")
        note_scroll = ttk.Scrollbar(right, command=self.note_text.yview)
        note_scroll.grid(row=0, column=1, sticky="ns")
        self.note_text.configure(yscrollcommand=note_scroll.set)
        # Text 的内边距：鼠标坐标换算成文本下标时要减掉它
        self._note_pad_x = 14
        self._note_pad_y = 12
        self._configure_note_tags()
        # 交互：左键点重试链接 / 点重点词展开；右键重点词删除
        self.note_text.bind("<Button-1>", self._on_note_click)
        self.note_text.bind("<Motion>", self._on_note_motion)
        self.note_text.bind("<Button-3>", self._on_note_context)

        # 底部操作提示（固定文字，不随鼠标变化）
        self.drop_hint = ttk.Label(
            right,
            text="点重点词后面的 ☆ 收藏到生词本；点词展开详情；右键可增删重点词或收藏",
            style="Hint.TLabel",
            anchor="center",
            padding=(4, 3),
        )
        self.drop_hint.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # ---- 底部：生成 / 导出 Word / 清空 ----
        bar = ttk.Frame(self.root, padding=(14, 4, 14, 10))
        bar.pack(fill="x")
        self.generate_btn = ttk.Button(bar, text="一键生成笔记", command=self.on_generate, width=16)
        self.generate_btn.pack(side="left", ipady=5)
        self.export_btn = ttk.Button(bar, text="导出 Word", command=self.on_export_docx, width=11)
        self.export_btn.pack(side="left", padx=(8, 0), ipady=5)
        self.md_btn = ttk.Button(bar, text="导出 Markdown", command=self.on_export_markdown, width=14)
        self.md_btn.pack(side="left", padx=6, ipady=5)
        self.anki_btn = ttk.Button(bar, text="导出 Anki CSV", command=self.on_export_anki, width=14)
        self.anki_btn.pack(side="left", ipady=5)
        self.book_btn = ttk.Button(bar, text="生词本", command=self.on_open_vocab_book, width=10)
        self.book_btn.pack(side="left", padx=(8, 0), ipady=5)
        ttk.Button(bar, text="清空", command=self.on_clear, width=8).pack(side="left", padx=6, ipady=5)

        status_bar = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        status_bar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪：把英文粘到左边，点「一键生成笔记」")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="determinate", length=200)
        self.progress.pack(side="right")
        # 状态栏右侧常驻显示收藏数量，点一下也能打开生词本
        self.vocab_status_var = tk.StringVar(value="")
        self.vocab_status = tk.Label(
            status_bar, textvariable=self.vocab_status_var, font=STAR_FONT,
            foreground="#c77700", cursor="hand2",
        )
        self.vocab_status.pack(side="right", padx=(0, 12))
        self.vocab_status.bind("<Button-1>", lambda e: self.on_open_vocab_book())

    def _configure_note_tags(self) -> None:
        w = self.note_text
        w.tag_configure("h1", font=("Microsoft YaHei UI", 13, "bold"), spacing1=4, spacing3=8)
        w.tag_configure("en", font=SERIF_FONT, lmargin1=4, lmargin2=4, spacing1=2, spacing3=6)
        w.tag_configure("zh", font=("Microsoft YaHei UI", 11), foreground="#1b5e20",
                        lmargin1=4, lmargin2=4, spacing1=2, spacing3=14)
        w.tag_configure("word", font=("Segoe UI", 12, "bold"), foreground="#0d47a1")
        # 可点击展开的重点词
        w.tag_configure("word_clickable", font=("Segoe UI", 12, "bold"), foreground="#0d47a1",
                        underline=False)
        w.tag_configure("word_clickable_hover", font=("Segoe UI", 12, "bold"),
                        foreground="#1565c0", underline=True)
        # 翻译失败时的重试链接
        w.tag_configure("retry", font=("Microsoft YaHei UI", 11, "bold"),
                        foreground="#b71c1c", underline=True)
        w.tag_configure("ipa", font=IPA_FONT, foreground="#1b5e20")
        # 翻译板块里的英文原文：重点词用绿色标出
        w.tag_configure("highlight", font=SERIF_FONT, foreground="#1b8a3a", background="#e8f5e9")
        w.tag_configure("dim", foreground="#888888")
        # 收藏按钮：空心 ☆ = 未收藏，实心 ★ = 已收藏（都是可点的）
        w.tag_configure("star", font=STAR_FONT, foreground="#b0a08a")
        w.tag_configure("star_on", font=STAR_FONT, foreground="#f0a020")
        w.tag_configure("star_hover", font=STAR_FONT, foreground="#e08a00", underline=True)
        w.tag_configure("bullet", lmargin1=16, lmargin2=30, spacing3=2)
        w.tag_configure("sep", foreground="#cccccc")

    # ---------------------------------------------------- 输入

    def _current_text(self) -> str:
        return self.input_text.get("1.0", "end-1c")

    def _show_input_hint(self) -> None:
        """原文框为空时，用灰色字显示一句提示（不是真的内容，不会被当成正文）。"""
        if self.input_text.get("1.0", "end-1c").strip():
            return
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", PLACEHOLDER)
        self.input_text.tag_add("plain_hint", "1.0", "end")
        self._placeholder_active = True

    def _clear_placeholder(self, event=None) -> None:
        """
        用户开始输入/粘贴时清掉灰色提示。
        只做一次删除，不会碰用户刚粘进来的内容 —— 这就是之前"粘贴后被清空"的根因修复点。
        """
        if not self._placeholder_active:
            return
        self._placeholder_active = False
        self.input_text.delete("1.0", "end")
        self.input_text.configure(foreground="#000000")

    def _on_input_changed(self, event=None) -> None:
        """输入框内容变化时：清掉灰色提示。"""
        if self._placeholder_active:
            self._clear_placeholder()

    def _on_input_focus_out(self, event=None) -> None:
        """离开输入框时如果还是空的，把提示放回来。"""
        if not self.input_text.get("1.0", "end-1c").strip():
            self._show_input_hint()

    # ---------------------------------------------------- 生成

    def on_generate(self) -> None:
        if self.busy:
            return
        text = self._current_text().strip()
        if not text:
            messagebox.showwarning("提示", "左边还没有英文内容，先粘贴一段再点吧～")
            return

        self.analysis = engine.analyze(text, self.lexicon)
        self.ai_summary = ""
        # 重新解析，手工改动也重置（生词本不受影响：收藏是长期的）
        self.added_words = set()
        self.removed_words = set()
        self.expanded_words = set()
        self._render_vocab_store()

        # 1) 本地词库命中的词（离线，必然有音标和释义）
        self.entry_lookups = {s.entry.word: s.entry for s in self.analysis.senses}

        # 2) 待联网补全的词
        candidates = engine.vocabulary_candidates(self.analysis, self.lexicon, limit=MAX_LOOKUP_WORDS)
        self.online_lookups = {}
        pending: List[str] = []
        for word, _ in candidates:
            cached = self.dict_cache.get(word)
            if cached and (cached.get("meaning") or cached.get("phonetic")):
                self.online_lookups[word] = dictionary.DictLookup(
                    word=word, phonetic=cached.get("phonetic", ""), pos=cached.get("pos", ""),
                    meaning=cached.get("meaning", ""), source="cache", ok=True,
                )
            else:
                pending.append(word)

        # 3) 段落译文：先看本地记忆/自动翻译缓存
        pending_paragraphs: List[str] = []
        for paragraph in self.analysis.paragraphs:
            key = paragraph.text.strip()
            cached = self.tm.get(key) or self.auto_cache.get(key)
            if cached:
                paragraph.translation = cached
            else:
                pending_paragraphs.append(key)

        # 4) 先把已有的内容画出来，用户马上有东西看
        self._render_note(translating=bool(pending_paragraphs), looking_up=bool(pending))
        self._set_status(
            f"已解析：{self.analysis.paragraph_count} 段 / {self.analysis.sentence_count} 句。"
            + ("正在翻译和查词……" if (pending_paragraphs or pending) else "全部命中本地缓存。")
        )

        if pending_paragraphs or pending:
            self._start_busy()
            threading.Thread(
                target=self._enrich_worker,
                args=(list(pending_paragraphs), list(pending)),
                daemon=True,
            ).start()
        else:
            self._finish("笔记已生成（全部来自本地缓存）。")

    def _enrich_worker(self, paragraphs: List[str], words: List[str]) -> None:
        """后台：翻段落 + 联网查词。任何一步失败都只是少点内容，不打断流程。"""
        try:
            total = len(paragraphs) + (1 if words else 0)
            done = 0
            translate_reason = ""

            for paragraph in paragraphs:
                result = trans.auto_translate(paragraph, cache=self.auto_cache)
                if result.ok:
                    self.msg_queue.put(("paragraph", (paragraph, result.text)))
                    # 部分成功（有块没翻出来）时 result.ok 也是真，但原因要说给用户听
                    if result.reason and not translate_reason:
                        translate_reason = result.reason
                elif result.reason and not translate_reason:
                    # 记住第一个失败原因，最后显示给用户（以前是静默吞掉的）
                    translate_reason = result.reason
                done += 1
                self.msg_queue.put(("progress", (done, total, "翻译和查词")))

            if words:
                def progress(inner_done: int, inner_total: int) -> None:
                    self.msg_queue.put(("progress", (done, total, "翻译和查词")))

                found = dictionary.enrich_words(
                    words, cache=self.dict_cache, max_words=MAX_LOOKUP_WORDS, progress=progress
                )
                if found:
                    self.msg_queue.put(("lookups", found))
                done += 1
                self.msg_queue.put(("progress", (done, total, "翻译和查词")))

            self.msg_queue.put(("enriched", translate_reason or None))
        except Exception as exc:
            self.msg_queue.put(("enriched", f"补充内容时出错（不影响其它部分）：{exc}"))

    def _handle_paragraph(self, paragraph_text: str, translation: str) -> None:
        if not self.analysis:
            return
        for paragraph in self.analysis.paragraphs:
            if paragraph.text.strip() == paragraph_text:
                paragraph.translation = translation
                self.auto_cache[paragraph_text] = translation
        self._render_note(translating=True)

    def _handle_lookups(self, found: Dict[str, dictionary.DictLookup]) -> None:
        self.online_lookups.update(found)
        self._render_note(translating=True)

    def _handle_enriched(self, error: Optional[str]) -> None:
        dictionary.save_cache(self.dict_cache)
        trans.save_auto_cache(self.auto_cache)
        self._render_note()
        self._end_busy()

        # 内容补齐后落一份 Word 到「英语笔记」文件夹
        path = self.export_docx_silently()
        suffix = f"　已生成 {os.path.basename(path)}" if path else ""

        if error:
            # error 是具体失败原因（比如额度用完 / 连不上），要明确告诉用户
            missing = sum(1 for p in self.analysis.paragraphs if not p.translation) if self.analysis else 0
            if missing:
                self._set_status(f"有 {missing} 段没拿到译文。原因：{error}{suffix}")
            else:
                # 没有整段缺译文，说明是"部分成功"：段落译出来了，但有分块没翻全
                self._set_status(f"笔记已生成，但翻译不完整：{error}{suffix}")
        else:
            missing = sum(1 for p in self.analysis.paragraphs if not p.translation) if self.analysis else 0
            if missing:
                self._set_status(
                    f"笔记已生成，但有 {missing} 段没拿到译文（接口没返回内容），其余内容正常。{suffix}"
                )
            else:
                self._set_status("笔记已生成：右边是逐段对照的翻译和重点词。" + suffix)

    # ---------------------------------------------------- 渲染单栏笔记

    def _clear_note(self) -> None:
        self.note_text.configure(state="normal")
        self.note_text.delete("1.0", "end")
        self.note_text.configure(state="disabled")

    def _write(self, content: str, tag: str = "") -> None:
        self.note_text.configure(state="normal")
        self.note_text.insert("end", content, tag) if tag else self.note_text.insert("end", content)
        self.note_text.configure(state="disabled")

    def _write_paragraph_with_highlights(self, text: str, key_words: set) -> None:
        """
        写入一段英文原文，把命中的重点词标成绿色。
        按"单词 / 非单词"交替切分，逐段写入，避免破坏原有空格和标点。
        """
        if not key_words:
            self._write(text + "\n", "en")
            return
        for token in re.findall(r"[A-Za-z][A-Za-z'’\-]*|[^A-Za-z]+", text):
            if token[:1].isalpha() and token.lower() in key_words:
                self._write(token, "highlight")
            else:
                self._write(token, "en")
        self._write("\n")

    def _render_note(self, translating: bool = False, looking_up: bool = False) -> None:
        """
        笔记只有两块：
          翻译   —— 每段英文下面直接接该段中文；翻译失败时只显示重试链接
          重点词 —— 高级词汇优先；点一下原地展开音标/释义/本文原句
        """
        if not self.analysis:
            return
        a = self.analysis
        self._clear_note()

        # 开头一行信息（不是板块，只是抬头）
        self._write(f"{a.paragraph_count} 段 · {a.sentence_count} 句 · {a.word_count} 词 · {a.difficulty_label}\n\n", "dim")

        # ============ 一、翻译（英文段落 + 紧接中文译文）============
        self._write("翻译\n", "h1")
        if self.ai_summary:
            self._write("· 大意：" + self.ai_summary.strip() + "\n\n", "zh")
        key_words = {row[0].lower() for row in self._key_word_rows()}
        for paragraph in a.paragraphs:
            # 段落编号只在多段时显示，单段时省略，界面更干净
            if a.paragraph_count > 1:
                self._write(f"第 {paragraph.index} 段\n", "dim")

            if paragraph.translation:
                # 有译文：英文原文（重点词绿色）→ 紧接中文
                self._write_paragraph_with_highlights(paragraph.text, key_words)
                self._write(paragraph.translation + "\n\n", "zh")
            elif translating:
                self._write_paragraph_with_highlights(paragraph.text, key_words)
                self._write("（正在翻译……）\n\n", "dim")
            else:
                # 翻译失败：不显示英文原文，只给一个可点击的重试链接
                self._write(f"[翻译失败，点击重试 · 第 {paragraph.index} 段]", "retry")
                self._write("\n\n")

        # ============ 二、重点词 ============
        rows = self._key_word_rows()
        saved = self.vocab_book.words()
        self._write(f"重点词（{len(rows)} 个）\n", "h1")
        if saved:
            self._write(f"　生词本里已经有 {len(saved)} 个词（点词后面的 ★ 可以取消收藏）。\n", "dim")
        self._write("　点一下词可以展开音标、释义和它在本文中的原句；再点收起。\n"
                    f"　点词后面的 ☆ 收藏到「{VOCAB_FOLDER_NAME}」（变成 ★），再点一下取消收藏。\n"
                    "　右键还能增删重点词、收藏整行。\n\n", "dim")

        if not rows:
            self._write("（没有重点词）\n", "dim")
        else:
            for row in rows:
                word = row[0]
                expanded = word.lower() in self.expanded_words
                starred = word.lower() in saved
                # 一行：· 单词　▸ 展开　★（★ / ☆ 可点，收藏或取消收藏）
                self._write("· ", "dim")
                self._write(word, "word_clickable")
                self._write("　" + ("▾ 收起" if expanded else "▸ 展开"), "dim")
                self._write("　")
                self._write(STAR_ON if starred else STAR_OFF, "star_on" if starred else "star")
                self._write("\n")
                if expanded:
                    self._write_word_detail(word, row)
                self._write("")

        if looking_up:
            self._write("（还有重点词正在联网查询……）\n", "dim")

    def _write_word_detail(self, word: str, row: tuple) -> None:
        """展开某个重点词时，写入它的音标、释义和本文原句。"""
        _, phonetic, pos, meaning, surface, extra = row
        if phonetic:
            self._write(f"    {phonetic}", "ipa")
        if pos or meaning:
            self._write(f"　{pos} {meaning}".rstrip() + "\n" if (pos or meaning) else "\n", "bullet")
        else:
            self._write("\n")
        if surface or extra:
            detail = "　".join(x for x in (surface, extra) if x)
            self._write(f"    {detail}\n", "dim")
        # 本文原句（从分析结果里直接取，不需要额外联网）
        for sentence in self._word_source_sentences(word):
            self._write(f"    原文：{sentence}\n", "en")
        self._write("\n")

    def _word_source_sentences(self, word: str, limit: int = 2) -> List[str]:
        """找出这个词在本文中出现的句子（最多 limit 句）。"""
        if not self.analysis:
            return []
        lower = word.lower()
        out: List[str] = []
        for sentence in self.analysis.sentences:
            for token in sentence.words:
                if token.lower() == lower or engine.strip_possessive(token).lower() == lower:
                    text = re.sub(r"\s+", " ", sentence.text).strip()
                    if text not in out:
                        out.append(text)
                    break
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------- 重点词的增删

    def _key_words(self) -> set:
        """当前生效的重点词集合（自动识别的 − 用户删掉的 + 用户勾选的）。"""
        words = {word for word, *_ in self._key_word_rows()}
        return words

    def _word_detail(self, word: str) -> Optional[Tuple[str, str, str, str, str, str]]:
        """
        取一个词的音标、词性、释义等，用于展示。
        先查内置词库，再查联网结果/缓存；都没有就返回占位行（仍可加入重点词）。
        """
        lower = word.lower()
        entry = self.lexicon.lookup(lower)
        if entry:
            surface = ""
            for sense in self.analysis.senses if self.analysis else []:
                if sense.entry.word == entry.word:
                    if sense.surface.lower() != entry.word:
                        surface = f"文中：{sense.surface}"
                    break
            return (entry.word, entry.phonetic, entry.pos, entry.meaning, surface, entry.label)

        found = self.online_lookups.get(lower)
        if found and found.ok:
            return (lower, found.phonetic, found.pos, found.meaning, "", "")
        cached = self.dict_cache.get(lower)
        if cached and (cached.get("meaning") or cached.get("phonetic")):
            return (lower, cached.get("phonetic", ""), cached.get("pos", ""), cached.get("meaning", ""), "", "")
        return (lower, "", "", NO_MEANING_HINT, "", "")

    def _key_word_rows(self) -> List[Tuple[str, str, str, str, str, str]]:
        """
        汇总重点词：内置词库命中的高难词 + 联网查到的高级词 + 用户勾选添加的，
        并去掉用户删掉的。排序按"高级程度"（词根/学术后缀/长度），高级词汇排前面。
        返回 [(单词, 音标, 词性, 释义, 文中形态, 附加说明), ...]
        """
        if not self.analysis:
            return []
        rows: List[Tuple[str, str, str, str, str, str]] = []
        seen = set()

        # 内置词库的高难词（六级及以上）
        for sense in self.analysis.unique_words:
            entry = sense.entry
            if not entry.is_hard:
                continue
            if entry.word in self.removed_words:
                continue
            seen.add(entry.word)
            surface = f"文中：{sense.surface}" if sense.is_inflected else ""
            rows.append((entry.word, entry.phonetic, entry.pos, entry.meaning, surface, entry.label))

        # 联网查到 / 内置词库收录但被勾选的词
        for word in sorted(self.online_lookups):
            found = self.online_lookups[word]
            if word in seen or not found.ok or word in self.removed_words:
                continue
            seen.add(word)
            rows.append((word, found.phonetic, found.pos, found.meaning, "", ""))

        # 用户勾选添加的
        for word in self.added_words:
            if word in seen or word in self.removed_words:
                continue
            seen.add(word)
            rows.append(self._word_detail(word))

        # 生词本里收藏过的词：即使这轮没被自动挑出来，也照样当重点词标绿 ——
        # 否则"收藏过了、下次却不认识"会很奇怪。用户在本文里明确删掉的仍然尊重。
        for word in sorted(self.vocab_book.words()):
            if word in seen or word in self.removed_words:
                continue
            seen.add(word)
            rows.append(self._word_detail(word))

        # 按高级程度排序
        def advanced_key(row: Tuple[str, str, str, str, str, str]) -> Tuple[float, int, str]:
            word = row[0].lower()
            return (-engine.advanced_score(word), -len(word), word)

        rows.sort(key=advanced_key)
        return rows

    # ---------------------------------------------------- 把原文的词拖进重点词

    def _word_from_event(self, event, widget=None) -> str:
        """取鼠标位置下的英文单词。"""
        widget = widget or self.input_text
        try:
            index = widget.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return ""
        line_text = widget.get(f"{index} linestart", f"{index} lineend")
        column = int(index.split(".")[1])
        for match in re.finditer(r"[A-Za-z][A-Za-z'’\-]*", line_text):
            if match.start() <= column <= match.end():
                return match.group(0)
        return ""

    def _star_state(self, word: str) -> Tuple[str, str]:
        """
        拼右键菜单里的收藏项：返回 (菜单文字, 是"取消收藏"吗)。
        同一个词在生词本里的形式可能和文中形式不同（analysed → analyse），
        所以三个候选都查一遍。
        """
        reserved = self.vocab_book.has(word)
        canonical = self._vocab_canonical(word)
        if canonical and self.vocab_book.has(canonical):
            reserved = True
        if reserved:
            return f"取消收藏（从生词本删除）：{canonical or word}", "取消"
        return f"收藏到生词本：{word}", "收藏"

    def _add_vocab_menu(self, menu: tk.Menu, word: str) -> None:
        """把"收藏 / 取消收藏"这一项加进右键菜单。"""
        label, action = self._star_state(word)
        if action == "取消":
            menu.add_command(label=label, command=lambda: self._unbookmark(word))
        else:
            menu.add_command(label=label, command=lambda: self._bookmark(word))

    def _on_input_context(self, event) -> None:
        """
        原文区右键菜单：把词加入重点词 / 收藏到生词本。
        右键时先把这个词临时标绿 —— 如果用户取消（没选加入），就恢复原样。
        """
        self._clear_placeholder()
        word = self._word_from_event(event)
        if not word:
            return

        # 先清掉上一次可能残留的临时标记，再标绿当前这个词
        self._clear_pending_add()
        self._mark_pending_add(event, word)

        allow, reason = self._addable(word)
        menu = tk.Menu(self.input_text, tearoff=0)
        if allow:
            in_key = word.lower() in {w.lower() for w in self._key_words()}
            if in_key:
                menu.add_command(label=f"「{word}」已在重点词里", state="disabled")
            else:
                menu.add_command(
                    label=f"加入重点词：{word}",
                    command=lambda: self._add_key_word(word),
                )
            self._add_vocab_menu(menu, word)
        else:
            menu.add_command(label=reason, state="disabled")
        menu.add_command(label="取消（恢复原样）", command=self._clear_pending_add)
        menu.add_separator()
        menu.add_command(label="复制这个单词", command=lambda: self._copy_word(word))
        menu.add_command(label="全选原文", command=lambda: self.input_text.tag_add("sel", "1.0", "end-1c"))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            # 菜单关闭后如果还留着临时标记（用户没选加入），恢复原样
            menu.grab_release()
            self.root.after(60, self._clear_pending_add)

    def _word_positions(self, word: str) -> List[Tuple[str, str]]:
        """找出一个词在原文里的所有位置（行.列 起止）。"""
        text = self._current_text()
        lowered = text.lower()
        target = word.lower()
        positions: List[Tuple[str, str]] = []
        start = 0
        while True:
            index = lowered.find(target, start)
            if index < 0:
                break
            before_ok = index == 0 or not lowered[index - 1].isalpha()
            after_ok = index + len(target) >= len(lowered) or not lowered[index + len(target)].isalpha()
            if before_ok and after_ok:
                line = text.count("\n", 0, index) + 1
                line_start = text.rfind("\n", 0, index) + 1
                column = index - line_start
                positions.append((f"{line}.{column}", f"{line}.{column + len(target)}"))
            start = index + len(target)
        return positions

    def _mark_pending_add(self, event, word: str) -> None:
        """把右键点到的那个词临时标绿（只标鼠标下这一处）。"""
        try:
            index = self.input_text.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        line_text = self.input_text.get(f"{index} linestart", f"{index} lineend")
        column = int(index.split(".")[1])
        for match in re.finditer(r"[A-Za-z][A-Za-z'’\-]*", line_text):
            if match.start() <= column <= match.end():
                line_no = index.split(".")[0]
                start = f"{line_no}.{match.start()}"
                end = f"{line_no}.{match.end()}"
                try:
                    self.input_text.tag_add("pending_add", start, end)
                    self._pending_add_index = (start, end)
                except tk.TclError:
                    pass
                return

    def _clear_pending_add(self) -> None:
        """撤销临时标绿（用户没有选择加入重点词时恢复原样）。"""
        try:
            self.input_text.tag_remove("pending_add", "1.0", "end")
        except tk.TclError:
            pass
        self._pending_add_index = None

    def _copy_word(self, word: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(word)
        self._set_status(f"已复制：{word}")

    def _auto_key_words(self) -> List[str]:
        """
        自动识别出来的重点词（不含用户手加的、也不含被用户删掉的）。
        注意：这里必须用"原始自动识别结果"来判断，
        否则删掉一个词库里的词后，它会被 is_hard 重新识别、导致删除失效。
        """
        if not self.analysis:
            return []
        words = []
        for sense in self.analysis.unique_words:
            if sense.entry.is_hard:
                words.append(sense.entry.word)
        for word, found in self.online_lookups.items():
            if found.ok:
                words.append(word)
        return words

    def _remove_key_word(self, word: str) -> None:
        """把某个词从重点词里删掉。"""
        lower = word.lower()
        self.removed_words.add(lower)
        self.added_words.discard(lower)
        self._render_note()
        self._set_status(f"已从重点词中删除：{lower}")

    # ---------------------------------------------------- 生词本（收藏重点词）

    def _vocab_row(self, word: str) -> Optional[tuple]:
        """在现在的重点词表里找这个词的详情行（找不到返回 None）。"""
        low = word.lower()
        for row in self._key_word_rows():
            if row[0].lower() == low:
                return row
        return None

    def _vocab_canonical(self, word: str) -> str:
        """
        收藏时用哪个形式当词条：词库里有原形就用原形（analysed → analyse），
        否则保持文中形式（只去掉所有格、去掉首尾空白）。
        """
        cleaned = engine.strip_possessive(word.strip()).strip()
        if not cleaned:
            return ""
        entry = self.lexicon.lookup(cleaned.lower())
        if entry:
            return entry.word
        return cleaned

    def _bookmark_toggle(self, word: str) -> None:
        """★ / ☆ 的点击入口：已收藏就取消，没收藏就收藏。"""
        if self.vocab_book.has(word):
            self._unbookmark(word)
        else:
            self._bookmark(word)

    def _bookmark(self, word: str, row: Optional[tuple] = None) -> None:
        """
        把一个词收藏进生词本。已经不存在的释义、原句都能补上 ——
        收藏是长期的，和"这一轮的重点词表"没有关系。
        """
        canonical = self._vocab_canonical(word)
        if not canonical:
            return
        row = row or self._vocab_row(canonical) or self._vocab_row(word)
        if row is None:
            detail = self._word_detail(canonical)
            if detail:
                row = detail
                canonical = row[0] or canonical
        entry = make_vocab_entry(canonical, row, self._word_source_sentences(canonical, limit=2))
        already = self.vocab_book.has(canonical)
        try:
            self.vocab_book.add(entry)
        except (ValueError, OSError) as exc:
            self._set_status(f"收藏失败：{exc}")
            return

        if already:
            self._set_status(f"「{canonical}」已在生词本里，这次只是补充了释义和原句。"
                             f"（共 {len(self.vocab_book)} 个）")
        else:
            self._set_status(f"已收藏到「{VOCAB_FOLDER_NAME}」：{canonical} "
                             f"（共 {len(self.vocab_book)} 个）")
        self._after_vocab_change()

    def _unbookmark(self, word: str) -> None:
        """把某个词从生词本里删掉（和 ☆ 同一个入口）。"""
        canonical = self._vocab_canonical(word)
        target = canonical if self.vocab_book.has(canonical) else word
        if not self.vocab_book.remove(target):
            return
        self._set_status(f"已从生词本里删除：{target}（还剩 {len(self.vocab_book)} 个）")
        self._after_vocab_change()

    def _after_vocab_change(self) -> None:
        """收藏变化后：刷新笔记里的 ★、状态栏数量、以及打开着的生词本窗口。"""
        self._render_vocab_store()
        self._render_note()
        if self.vocab_dialog is not None:
            self._sync_vocab_dialog()

    def _sync_vocab_dialog(self) -> None:
        """生词本窗口开着时，把里面的列表和标题跟着刷新。"""
        dialog = self.vocab_dialog
        if dialog is None:
            return
        try:
            if not dialog.winfo_exists():
                self.vocab_dialog = None
                return
            dialog._fill()
        except tk.TclError:
            self.vocab_dialog = None

    def _render_vocab_store(self) -> None:
        """状态栏右侧常驻的收藏数量（点一下也能打开生词本）。"""
        count = len(self.vocab_book)
        self.vocab_status_var.set(
            f"{STAR_ON} 生词本：{count} 个" if count else f"{STAR_OFF} 生词本：空"
        )
        if hasattr(self, "vocab_btn"):
            self.vocab_btn.configure(text=f"生词本 {count}")

    def on_open_vocab_book(self) -> None:
        """打开生词本窗口（已经开着就把它提到前面）。"""
        if self.vocab_dialog is not None:
            try:
                if self.vocab_dialog.winfo_exists():
                    self.vocab_dialog.deiconify()
                    self.vocab_dialog.lift()
                    self.vocab_dialog.focus_force()
                    return
            except tk.TclError:
                pass
            self.vocab_dialog = None

        dialog = VocabBookDialog(self.root, self.vocab_book, on_change=self._on_vocab_book_changed)
        self.vocab_dialog = dialog
        dialog._center_on(self.root)
        self._set_status(f"生词本：{len(self.vocab_book)} 个词，保存在「{VOCAB_FOLDER_NAME}」文件夹里。")

    def _on_vocab_book_changed(self) -> None:
        """生词本窗口里删了词之后，主窗口要跟着更新（★ 和数量）。"""
        self._render_vocab_store()
        if self.analysis:
            self._render_note()

    # ---------------------------------------------------- 笔记区交互

    def _tag_at(self, index: str, tag: str) -> bool:
        """判断某个下标是否落在指定标签范围内。"""
        try:
            return tag in self.note_text.tag_names(index)
        except tk.TclError:
            return False

    def _on_note_click(self, event) -> None:
        """
        笔记区左键：
          · 点 ☆ / ★ → 收藏这个重点词 / 取消收藏
          · 点在 [翻译失败，点击重试 · 第 N 段] 上 → 只重翻那一段
          · 点在重点词上 → 展开/收起它的详情
        注意：Text 的 @x,y 索引用的是控件坐标（已含 padding），
        不能用 dlineinfo 的偏移去二次换算，直接传事件坐标即可。
        """
        index = self.note_text.index(f"@{event.x},{event.y}")

        # 0) 收藏按钮（☆ / ★）—— 必须排在"展开详情"前面，
        #    因为它和单词在同一行，否则点星星会变成展开。
        if self._tag_at(index, "star") or self._tag_at(index, "star_on"):
            word = self._word_at_index(index)
            if word:
                self._bookmark_toggle(word)
            return

        # 1) 重试链接
        if self._tag_at(index, "retry"):
            paragraph_index = self._retry_paragraph_index_at(index)
            if paragraph_index:
                self._retry_paragraph(paragraph_index)
            return

        # 2) 重点词展开/收起
        # 点在「·」或词前空白上时，tag 不是 word_clickable，
        # 但这种"差一点点"的点击应该也算展开，否则手感很差。
        if self._tag_at(index, "word_clickable") or self._looks_like_keyword_line(index):
            word = self._word_at_index(index)
            if word and word.lower() in {w.lower() for w in self._key_words()}:
                self._toggle_word_detail(word)

    def _looks_like_keyword_line(self, index: str) -> bool:
        """判断这一行是不是重点词条目行（以「·」开头）。"""
        line_text = self.note_text.get(f"{index} linestart", f"{index} lineend")
        return line_text.lstrip().startswith("·")

    def _retry_paragraph_index_at(self, index: str) -> Optional[int]:
        """从重试链接所在行解析出段落序号。"""
        line_text = self.note_text.get(f"{index} linestart", f"{index} lineend")
        match = re.search(r"第\s*(\d+)\s*段", line_text)
        return int(match.group(1)) if match else None

    def _toggle_word_detail(self, word: str) -> None:
        """展开或收起一个重点词的详情。"""
        lower = word.lower()
        if lower in self.expanded_words:
            self.expanded_words.discard(lower)
        else:
            self.expanded_words.add(lower)
        self._render_note()

    def _on_note_motion(self, event) -> None:
        """鼠标移到可点击元素上时换成手型光标，给出可点的提示。"""
        index = self.note_text.index(f"@{event.x},{event.y}")
        clickable = (self._tag_at(index, "retry")
                     or self._tag_at(index, "star")
                     or self._tag_at(index, "star_on")
                     or self._tag_at(index, "word_clickable")
                     or self._looks_like_keyword_line(index))
        try:
            self.note_text.configure(cursor="hand2" if clickable else "")
        except tk.TclError:
            pass

    # ---------------------------------------------------- 单段重试翻译

    def _retry_paragraph(self, paragraph_index: int) -> None:
        """只重新翻译某一段（不重新生成整篇）。"""
        if self.busy or self.retrying_paragraphs:
            self._set_status("正在重试翻译，请稍等……")
            return
        if not self.analysis or not (1 <= paragraph_index <= self.analysis.paragraph_count):
            return
        paragraph = self.analysis.paragraphs[paragraph_index - 1]
        text = paragraph.text.strip()
        if not text:
            return

        # 之前失败的段落可能留下了"空译文"缓存，清掉再试
        self.auto_cache.pop(text, None)
        self.retrying_paragraphs.add(paragraph_index)
        # 重试期间置为忙碌：防止重复点击，也让按钮状态一致
        self._start_busy()
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._set_status(f"正在重试第 {paragraph_index} 段的翻译……")
        threading.Thread(
            target=self._retry_worker, args=(paragraph_index, text), daemon=True
        ).start()

    def _retry_worker(self, paragraph_index: int, text: str) -> None:
        try:
            result = trans.auto_translate(text, cache=self.auto_cache, use_cache=False)
            self.msg_queue.put(("retry_result", (paragraph_index, result)))
        except Exception as exc:
            self.msg_queue.put(("retry_result", (paragraph_index,
                                                 trans.AutoResult(reason=f"重试出错：{exc}"))))

    def _handle_retry_result(self, paragraph_index: int, result) -> None:
        self.retrying_paragraphs.discard(paragraph_index)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._end_busy()
        if result.ok:
            if self.analysis and 1 <= paragraph_index <= self.analysis.paragraph_count:
                self.analysis.paragraphs[paragraph_index - 1].translation = result.text
            trans.save_auto_cache(self.auto_cache)
            self._render_note()
            if result.reason:
                self._set_status(f"第 {paragraph_index} 段补上了一部分：{result.reason}")
            else:
                self._set_status(f"第 {paragraph_index} 段翻译成功。")
        else:
            self._render_note()
            self._set_status(f"第 {paragraph_index} 段重试仍失败：{result.reason}")

    def _word_at_index(self, index: str) -> str:
        """
        取出某个下标处的英文单词。
        按列取词；点到分隔符（· 或全角空格）时，取**离它最近**的那个词，
        而不是无脑退回行首词 —— 否则点空白会操作到一个不相干的词。
        """
        line_text = self.note_text.get(f"{index} linestart", f"{index} lineend")
        column = int(index.split(".")[1])
        words = list(re.finditer(r"[A-Za-z][A-Za-z'’\-]*", line_text))
        if not words:
            return ""
        for match in words:
            if match.start() <= column <= match.end():
                return match.group(0)
        # 落在词与词之间：取开头离点击位置最近的那个
        best = min(words, key=lambda m: min(abs(m.start() - column), abs(m.end() - column)))
        return best.group(0)

    def _on_note_context(self, event) -> None:
        """
        笔记区右键菜单。
        重点词条目上右键 → 删除 / 收藏；原文行里的词右键 → 也能删改（如果它是重点词）。
        """
        index = self.note_text.index(f"@{event.x},{event.y}")
        line_text = self.note_text.get(f"{index} linestart", f"{index} lineend")
        word = self._word_at_index(index)
        if not word:
            return

        low = word.lower()
        # 这个词在当前重点词里的"规范形式"（原文里可能是变形，如 analysed → analyse）
        canonical = low
        entry = self.lexicon.lookup(low)
        if entry:
            canonical = entry.word
        in_key = canonical in {w.lower() for w in self._key_words()}

        menu = tk.Menu(self.note_text, tearoff=0)
        if line_text.lstrip().startswith("·") or in_key:
            menu.add_command(
                label=f"从重点词中删除：{word}",
                command=lambda: self._remove_key_word(canonical),
            )
            self._add_vocab_menu(menu, canonical)
        else:
            menu.add_command(label=f"「{word}」不是重点词", state="disabled")
            menu.add_command(
                label=f"加入重点词：{word}",
                command=lambda: self._add_key_word(word),
            )
            self._add_vocab_menu(menu, word)
        menu.add_separator()
        menu.add_command(label="打开生词本", command=self.on_open_vocab_book)
        menu.add_command(label=f"查词：{word}", command=self.on_lookup_word)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _addable(self, word: str) -> Tuple[bool, str]:
        """
        判断一个词值不值得加进重点词。

        拦三种情况：
          · 太短（1-2 个字母）
          · 虚词 / 代词（in / the / its / their / this 这类）
          · 所有格（corporation's、workers'）—— 重点词只收名词本身
        其它一律尊重用户的选择 —— 用户明确右键选的词就该加进去
        （即使是派生词，比如 presented，也可能正是他想记的）。
        """
        raw = word.strip()
        lower = engine.strip_possessive(raw).lower()
        if engine.is_possessive(raw):
            return False, f"「{raw}」是所有格，请选它的原形「{lower}」"
        if len(lower) < 3:
            return False, f"「{word}」太短，不是值得记的词"
        if lower in engine.STOPWORDS:
            return False, f"「{word}」是虚词或代词，不用记"
        return True, ""

    def _add_key_word(self, word: str) -> None:
        """把词加入重点词（并尽量补上音标释义）。"""
        lower = word.lower().strip()
        if not lower:
            return
        allow, reason = self._addable(lower)
        if not allow:
            self._set_status(reason)
            return
        self.added_words.add(lower)
        self.removed_words.discard(lower)
        # 内置词库没有、本地缓存也没有的词，顺手联网查一下
        if not self.lexicon.lookup(lower) and lower not in self.dict_cache:
            found = dictionary.lookup_youdao(lower)
            if found.ok:
                self.online_lookups[lower] = found
                self.dict_cache[lower] = found.as_cache()
                dictionary.save_cache(self.dict_cache)
        self._render_note()
        self._set_status(f"已加入重点词：{lower}")

    # ---------------------------------------------------- 队列

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "progress":
                    done, total, label = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"{label}… {done}/{total}")
                elif kind == "paragraph":
                    self._handle_paragraph(*payload)
                elif kind == "lookups":
                    self._handle_lookups(payload)
                elif kind == "enriched":
                    self._handle_enriched(payload)
                elif kind == "retry_result":
                    self._handle_retry_result(*payload)
                elif kind == "ai_summary":
                    self.ai_summary = str(payload)
                    self._render_note()
                    self._end_busy()
                    self._set_status("大意已由 AI 归纳好，显示在「翻译」那一段的开头。")
                elif kind == "ai_summary_failed":
                    self._end_busy()
                    self._set_status(f"AI 归纳失败（不影响其它内容）：{payload}")
                elif kind == "error":
                    self._end_busy()
                    messagebox.showerror("出错了", str(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    # ---------------------------------------------------- 状态

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _start_busy(self) -> None:
        self.busy = True
        self.generate_btn.configure(state="disabled")
        self.progress.configure(value=0)

    def _end_busy(self) -> None:
        self.busy = False
        self.generate_btn.configure(state="normal")
        self.progress.configure(value=0)

    def _finish(self, message: str) -> None:
        self._render_note()
        # 每次生成都同时落一份 Word 到程序所在文件夹
        path = self.export_docx_silently()
        if path:
            message += f"　已生成 {os.path.basename(path)}"
        self._set_status(message)

    # ---------------------------------------------------- 导出 / 清空 / 其它

    def _timestamped_docx_path(self) -> str:
        """
        在程序所在文件夹下的「英语笔记」文件夹里生成带时间戳的文件名，
        不会覆盖之前生成的笔记：
            英语笔记/English_Notes_20250131_142530.docx
        """
        return self._timestamped_path(".docx")

    def on_export_markdown(self) -> None:
        """导出 Markdown（给 Obsidian / Notion / Typora 用）。"""
        if not self.analysis:
            messagebox.showinfo("提示", "请先点「一键生成笔记」。")
            return
        path = self._timestamped_path(".md")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._build_markdown())
        except OSError as exc:
            messagebox.showerror("保存失败", f"写文件时出错：{exc}")
            return
        self._set_status(f"已导出 Markdown：{os.path.basename(path)}（在「{NOTES_FOLDER_NAME}」文件夹里）")

    def on_export_anki(self) -> None:
        """导出 Anki 可导入的 CSV：正面=单词，背面=音标+释义+本文原句。"""
        if not self.analysis:
            messagebox.showinfo("提示", "请先点「一键生成笔记」。")
            return
        path = self._timestamped_path(".csv")
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(self._build_anki_csv())
        except OSError as exc:
            messagebox.showerror("保存失败", f"写文件时出错：{exc}")
            return
        self._set_status(f"已导出 Anki CSV：{os.path.basename(path)}（在「{NOTES_FOLDER_NAME}」文件夹里）")

    def _timestamped_path(self, suffix: str) -> str:
        """在「英语笔记」文件夹里生成带时间戳的文件路径（后缀可变）。"""
        folder = os.path.join(BASE_DIR, NOTES_FOLDER_NAME)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(folder, f"English_Notes_{stamp}{suffix}")

    def _build_markdown(self) -> str:
        """把当前笔记（含你调整过的重点词）组织成 Markdown。"""
        a = self.analysis
        rows = self._key_word_rows()
        lines: List[str] = ["# 英语笔记", ""]
        lines.append(f"> {a.paragraph_count} 段 · {a.sentence_count} 句 · {a.word_count} 词 · {a.difficulty_label}")
        lines.append("")

        lines.append("## 翻译（逐段对照）")
        lines.append("")
        for paragraph in a.paragraphs:
            if a.paragraph_count > 1:
                lines.append(f"### 第 {paragraph.index} 段")
                lines.append("")
            lines.append(paragraph.text)
            lines.append("")
            lines.append(f"> {paragraph.translation}" if paragraph.translation
                         else "> [翻译失败，请重试]")
            lines.append("")

        if self.ai_summary:
            lines.append("## 大意")
            lines.append("")
            lines.append(self.ai_summary.strip())
            lines.append("")

        lines.append(f"## 重点词（{len(rows)} 个）")
        lines.append("")
        for word, phonetic, pos, meaning, surface, extra in rows:
            lines.append(f"### {word}")
            lines.append("")
            if phonetic or pos or meaning:
                lines.append(f"- 音标：`{phonetic or '—'}`")
                lines.append(f"- 词性释义：{pos} {meaning}".rstrip())
            if surface or extra:
                lines.append(f"- 备注：{'　'.join(x for x in (surface, extra) if x)}")
            for sentence in self._word_source_sentences(word):
                lines.append(f"- 本文原句：{sentence}")
            lines.append("")

        lines.append("## 原文")
        lines.append("")
        lines.append(a.text)
        lines.append("")
        return "\n".join(lines)

    def _build_anki_csv(self) -> str:
        """
        Anki CSV：三列 = 正面 / 背面 / 标签。
        用 csv 模块处理引号转义，避免释义里的逗号把列冲散。
        """
        import csv
        import io

        rows = self._key_word_rows()
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
        writer.writerow(["Front", "Back", "Tags"])
        for word, phonetic, pos, meaning, surface, extra in rows:
            back_parts = []
            if phonetic:
                back_parts.append(phonetic)
            if pos or meaning:
                back_parts.append(f"{pos} {meaning}".strip())
            if surface or extra:
                back_parts.append("　".join(x for x in (surface, extra) if x))
            sentences = self._word_source_sentences(word, limit=1)
            if sentences:
                back_parts.append(f"例：{sentences[0]}")
            writer.writerow([word, "<br>".join(back_parts), "英语笔记"])
        return buffer.getvalue()

    def on_export_docx(self) -> None:
        """
        底部「导出 Word」按钮：把**当前**笔记（含你刚勾选/删除的重点词）
        存进程序所在文件夹的「英语笔记」文件夹。
        """
        if not self.analysis:
            messagebox.showinfo("提示", "请先点「一键生成笔记」。")
            return
        path = self.export_docx_silently()
        if path:
            self._set_status(f"已导出 Word：{os.path.basename(path)}（在「{NOTES_FOLDER_NAME}」文件夹里）")

    def export_docx_silently(self) -> Optional[str]:
        """
        不弹保存对话框，直接导出到程序所在文件夹。
        成功返回文件路径；失败时返回 None（缺 python-docx 会提示一次）。
        """
        if not self.analysis:
            return None
        path = self._timestamped_docx_path()
        try:
            self._write_docx(path)
        except ImportError:
            if not self._docx_warned:
                self._docx_warned = True
                messagebox.showwarning(
                    "Word 没能生成",
                    "生成 Word 需要一个库：\n\n    pip install python-docx\n\n"
                    "装好后再点一次「一键生成笔记」即可。\n"
                    "（不影响界面上的笔记内容）",
                )
            return None
        except Exception as exc:
            if not self._docx_warned:
                self._docx_warned = True
                messagebox.showwarning("Word 没能生成", f"生成 Word 时出错：{exc}")
            return None
        return path

    def _write_docx(self, path: str) -> None:
        from docx import Document

        a = self.analysis
        doc = Document()
        doc.add_heading("英语笔记", level=0)

        # 重点词集合：用于把 Word 里的英文原文标绿
        key_words = {row[0].lower() for row in self._key_word_rows()}

        doc.add_heading("翻译", level=1)
        if self.ai_summary:
            doc.add_paragraph("大意：" + self.ai_summary.strip())
        for paragraph in a.paragraphs:
            if a.paragraph_count > 1:
                doc.add_paragraph(f"第 {paragraph.index} 段", style="Intense Quote")
            # 英文原文：重点词标绿（绿字 + 浅绿底）
            add_highlighted_paragraph(doc, paragraph.text, key_words)
            doc.add_paragraph(paragraph.translation or "（没有译文）")

        doc.add_heading("重点词", level=1)
        rows = self._key_word_rows()
        if rows:
            table = doc.add_table(rows=1, cols=4)
            table.style = "Light Grid Accent 1"
            header = table.rows[0].cells
            for i, name in enumerate(["单词", "音标", "词性", "释义"]):
                header[i].text = name
            for word, phonetic, pos, meaning, _, _ in rows:
                cells = table.add_row().cells
                cells[0].text = word
                cells[1].text = phonetic or "—"
                cells[2].text = pos or "—"
                cells[3].text = meaning
        else:
            doc.add_paragraph("（没有识别到重点词）")

        doc.save(path)

    def on_clear(self) -> None:
        self._placeholder_active = False
        self.input_text.delete("1.0", "end")
        self.analysis = None
        self.online_lookups = {}
        self.entry_lookups = {}
        self.ai_summary = ""
        self.added_words = set()
        self.removed_words = set()
        self.expanded_words = set()
        self.retrying_paragraphs = set()
        self._clear_note()
        # 生词本不清空 —— 收藏是长期资料，要清得去生词本窗口里删
        self._set_status("已清空。（生词本里的收藏不受影响）")

    def on_open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.ai_config)
        self.root.wait_window(dialog)
        if dialog.result:
            self.ai_config = dialog.result
            self.config_data.update(self.ai_config.public_view())
            save_config(self.config_data)
            self._set_status("设置已保存。")
            # 填了 AI 密钥就顺手更新一次大意
            if self.analysis and self.ai_config.llm_ready():
                self._start_ai_summary()

    def _start_ai_summary(self) -> None:
        if self.busy or not self.analysis:
            return
        self._start_busy()
        self._set_status("正在让 AI 归纳大意……")
        threading.Thread(target=self._ai_worker, daemon=True).start()

    def _ai_worker(self) -> None:
        try:
            payload = "\n\n".join(f"[Paragraph {p.index}]\n{p.text}" for p in self.analysis.paragraphs)
            prompt = engine.SUMMARY_SYSTEM_PROMPT + " 请只输出中文概括，不要序号。"
            result = trans.summarize(payload, self.ai_config, prompt)
            self.msg_queue.put(("ai_summary", result))
        except Exception as exc:
            self.msg_queue.put(("ai_summary_failed", str(exc)))

    def on_lookup_word(self) -> None:
        word = ""
        for widget in (self.root.focus_get(), self.note_text, self.input_text):
            if isinstance(widget, tk.Text):
                try:
                    word = widget.get("sel.first", "sel.last").strip()
                except tk.TclError:
                    word = ""
                if word:
                    break
        if not word:
            word = simpledialog.askstring("查词", "输入要查的英文单词：", parent=self.root) or ""
        word = word.strip()
        if not word:
            return

        entry = self.lexicon.lookup(word)
        if entry:
            messagebox.showinfo(f"查词：{word}", f"{entry.word}\n    {entry.phonetic}  {entry.pos} {entry.meaning}")
            return

        # 联网查
        found = self.online_lookups.get(word.lower()) or self.dict_cache.get(word.lower())
        if not found:
            lookup = dictionary.lookup_youdao(word)
            if lookup.ok:
                self.dict_cache[word.lower()] = lookup.as_cache()
                dictionary.save_cache(self.dict_cache)
                messagebox.showinfo(f"查词：{word}", f"{lookup.word}\n    {lookup.phonetic}  {lookup.pos} {lookup.meaning}")
            else:
                messagebox.showinfo("查不到", f"没查到「{word}」，可能拼写有误或者网络不通。")
            return
        if isinstance(found, dictionary.DictLookup):
            messagebox.showinfo(f"查词：{word}", f"{found.word}\n    {found.phonetic}  {found.pos} {found.meaning}")
        else:
            messagebox.showinfo(f"查词：{word}", f"{word}\n    {found.get('phonetic','')}  {found.get('meaning','')}")

    def _on_close(self) -> None:
        try:
            dictionary.save_cache(self.dict_cache)
            trans.save_auto_cache(self.auto_cache)
            engine.save_tm(self.tm)
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    EnglishNoteApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
