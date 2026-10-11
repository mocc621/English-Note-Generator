# -*- coding: utf-8 -*-
"""
vocab_book.py —— 生词本（收藏的重点词）

收藏的重点词不放在内存里，而是**立刻落盘**到这个文件：
    生词本/生词本.json       ← 数据源（程序读它、写它）
    生词本/生词本.md         ← 同一个内容的可读版（随时自动重写）

设计要点（为什么要这样）：
  1. 只依赖标准库 —— 生词本是"你的资料"，不能因为缺库就读不出来。
  2. JSON 是唯一数据源，Markdown 是自动生成的"给人看的副本"，
     所以 Markdown 可以放心地长期保存、放进 Obsidian、随便复制分享。
  3. 收藏是**跨次生效**的：点「一键生成笔记」只重置重点词的临时增删，
     不会动生词本。反过来，生词本里的词会重新算进笔记的绿色标注。
  4. 写文件先写临时文件再替换（os.replace），
     避免写到一半断电/报错，把攒了很久的生词本弄坏。

只依赖标准库。
"""

import csv
import datetime
import io
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 收藏的词全部保存在程序所在文件夹的这个子文件夹里
FOLDER_NAME = "生词本"
FOLDER = os.path.join(BASE_DIR, FOLDER_NAME)
DATA_FILE = os.path.join(FOLDER, "生词本.json")
MARKDOWN_FILE = os.path.join(FOLDER, "生词本.md")

# Markdown 里的标题与表头用得上
MARKDOWN_TITLE = "# 生词本"
ANKI_TAG = "英语笔记 生词本"

# 收藏成功时统一用这个前缀生成文件名：Vocabulary_20261011_110400.md
FILE_PREFIX = "Vocabulary"

# 一条生词最多留几句本文原句（多了反而占地方）
MAX_SENTENCES = 2


# ============================================================ 数据结构


@dataclass
class VocabEntry:
    """生词本里的一条收藏。"""

    word: str                      # 规范形式（一般是小写原形）
    phonetic: str = ""             # 形如 /juːˈbɪkwɪtəs/
    pos: str = ""                  # 形如 adj.
    meaning: str = ""              # 中文释义
    sentences: List[str] = field(default_factory=list)   # 本文原句
    note: str = ""                 # 备注（文中形态等）
    tags: List[str] = field(default_factory=list)        # 考试标签，如 雅思/考研
    added_at: str = ""             # 首次收藏时间
    updated_at: str = ""           # 最近一次收藏时间

    @property
    def key(self) -> str:
        """去重用的键：一律小写。"""
        return self.word.strip().lower()

    @property
    def added_date(self) -> str:
        """只取日期部分，列表里显示更短。"""
        return (self.added_at or "").split(" ")[0]

    def display(self) -> str:
        """一行式的摘要，用于状态栏 / 列表。"""
        detail = "　".join(x for x in (self.phonetic, f"{self.pos} {self.meaning}".strip()) if x.strip())
        return f"{self.word}　{detail}".strip()

    def as_dict(self) -> dict:
        return {
            "word": self.word,
            "phonetic": self.phonetic,
            "pos": self.pos,
            "meaning": self.meaning,
            "sentences": list(self.sentences),
            "note": self.note,
            "tags": list(self.tags),
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VocabEntry":
        """从 JSON 里读一条。字段缺失 / 类型不对都要能扛住（旧文件、手改过的文件）。"""
        word = str(data.get("word", "")).strip()
        sentences = data.get("sentences") or []
        if isinstance(sentences, str):
            sentences = [sentences]
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return cls(
            word=word,
            phonetic=str(data.get("phonetic", "") or ""),
            pos=str(data.get("pos", "") or ""),
            meaning=str(data.get("meaning", "") or ""),
            sentences=[str(s).strip() for s in sentences if str(s).strip()],
            note=str(data.get("note", "") or ""),
            tags=[str(t).strip() for t in tags if str(t).strip()],
            added_at=str(data.get("added_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


def now_stamp() -> str:
    """形如 2026-10-11 11:03:15。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================ 合并两条收藏


def _merge_sentences(old: Sequence[str], new: Sequence[str]) -> List[str]:
    """原句去重后合并，最多留 MAX_SENTENCES 句（顺序保持：先收藏的排前面）。"""
    out: List[str] = []
    for item in list(old) + list(new):
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out[:MAX_SENTENCES]


def merge_entry(old: VocabEntry, new: VocabEntry, stamp: Optional[str] = None) -> VocabEntry:
    """
    把新收藏合并到已有条目上：**补齐缺失的字段，不覆盖已有的内容**。
    这样重复收藏同一个词是安全的：只会让释义更全、原句更多。
    """
    stamp = stamp or now_stamp()
    return VocabEntry(
        word=old.word or new.word,
        phonetic=old.phonetic or new.phonetic,
        pos=old.pos or new.pos,
        meaning=old.meaning or new.meaning,
        sentences=_merge_sentences(old.sentences, new.sentences),
        note=old.note or new.note,
        tags=list(dict.fromkeys(list(old.tags) + list(new.tags))),
        added_at=old.added_at or new.added_at or stamp,
        updated_at=stamp,
    )


# ============================================================ 生词本


class Store:
    """
    生词本的内存视图 + 持久化。

    entries 的顺序就是"收藏顺序"（先收藏的在前面），
    界面想按时间倒序显示时自己反转一下即可，数据文件保持稳定顺序。
    """

    DATA_NAME = "生词本.json"
    MARKDOWN_NAME = "生词本.md"

    def __init__(self, entries: Optional[Iterable[VocabEntry]] = None,
                 folder: str = FOLDER):
        self.folder = folder
        self.data_file = os.path.join(folder, self.DATA_NAME)
        self.markdown_file = os.path.join(folder, self.MARKDOWN_NAME)
        self.entries: List[VocabEntry] = []
        for entry in entries or []:
            if entry and entry.word:
                self.entries.append(entry)

    # ---------------------------------------------------- 查询

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, word: object) -> bool:
        return self.has(str(word))

    def has(self, word: str) -> bool:
        key = str(word).strip().lower()
        return any(e.key == key for e in self.entries)

    def get(self, word: str) -> Optional[VocabEntry]:
        key = str(word).strip().lower()
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None

    def words(self) -> set:
        """所有收藏词的集合（小写），可直接拿去做绿色标注 / 命中判断。"""
        return {e.key for e in self.entries}

    def newest_first(self) -> List[VocabEntry]:
        """按"最近收藏"倒序 —— 刚刚收藏的词排在最前面，方便马上核对。"""
        return sorted(self.entries, key=lambda e: (e.updated_at or "", e.added_at or ""), reverse=True)

    # ---------------------------------------------------- 增删

    def add(self, entry: VocabEntry, folder: Optional[str] = None) -> VocabEntry:
        """
        收藏一个词。已经在生词本里的词不会重复添加，只会补齐释义和原句。
        返回最终保存下来的那条，并立刻写盘（JSON + Markdown）。
        """
        stamp = now_stamp()
        key = entry.key
        if not key:
            raise ValueError("空的单词不能收藏")
        existing = self.get(key)
        if existing is None:
            saved = merge_entry(entry, entry, stamp=stamp)
            if not saved.added_at:
                saved.added_at = stamp
            self.entries.append(saved)
        else:
            saved = merge_entry(existing, entry, stamp=stamp)
            self.entries[self.entries.index(existing)] = saved
        self.save(folder=folder)
        return saved

    def remove(self, word: str, folder: Optional[str] = None) -> bool:
        """从生词本里删掉一个词。删到了返回 True。"""
        key = str(word).strip().lower()
        for entry in list(self.entries):
            if entry.key == key:
                self.entries.remove(entry)
                self.save(folder=folder)
                return True
        return False

    def clear(self, folder: Optional[str] = None) -> None:
        self.entries = []
        self.save(folder=folder)

    # ---------------------------------------------------- 持久化

    def save(self, folder: Optional[str] = None) -> None:
        """
        写「生词本.json」（数据源）和「生词本.md」（可读副本）。
        任何写失败都只是没保存，不该让界面崩掉。
        """
        target_folder = folder or self.folder
        data_file = os.path.join(target_folder, self.DATA_NAME)
        markdown_file = os.path.join(target_folder, self.MARKDOWN_NAME)
        try:
            os.makedirs(target_folder, exist_ok=True)
        except OSError:
            return

        payload = {
            "version": 1,
            "app": "英语笔记生成器",
            "updated_at": now_stamp(),
            "count": len(self.entries),
            "words": [e.as_dict() for e in self.entries],
        }
        _atomic_write(data_file, json.dumps(payload, ensure_ascii=False, indent=2))
        _atomic_write(markdown_file, render_markdown(self.entries))

    @classmethod
    def load(cls, folder: str = FOLDER) -> "Store":
        """
        读生词本。文件不存在（或第一次用）时返回空生词本，不报错也不建文件 ——
        真正收藏第一个词时才创建「生词本」文件夹。
        """
        path = os.path.join(folder, cls.DATA_NAME)
        store = cls(folder=folder)
        if not os.path.exists(path):
            return store
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return store

        raw = data.get("words") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return store
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            entry = VocabEntry.from_dict(item)
            if not entry.word or entry.key in seen:
                continue
            seen.add(entry.key)
            store.entries.append(entry)
        return store


def _atomic_write(path: str, text: str) -> None:
    """先写 .tmp 再替换，避免写到一半把原文件弄坏。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ============================================================ 文件夹 / 文件名


def ensure_folder(folder: str = FOLDER) -> str:
    """确保「生词本」文件夹存在（手动删掉也能自愈），返回它的路径。"""
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return folder


def open_folder(folder: str = FOLDER) -> bool:
    """用资源管理器打开「生词本」文件夹。"""
    ensure_folder(folder)
    try:
        os.startfile(folder)     # type: ignore[attr-defined]  # 仅 Windows 有
        return True
    except (AttributeError, OSError):
        return False


def timestamped_path(suffix: str, folder: str = FOLDER) -> str:
    """在「生词本」文件夹里生成带时间戳的导出路径（不会覆盖旧文件）。"""
    ensure_folder(folder)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(folder, f"{FILE_PREFIX}_{stamp}{suffix}")


# ============================================================ 导出用的文本


def render_markdown(entries: Sequence[VocabEntry]) -> str:
    """把生词本渲染成一份能直接读的 Markdown。"""
    lines: List[str] = [MARKDOWN_TITLE, ""]
    lines.append(f"> 共 {len(entries)} 个词 · 收藏时间：{now_stamp()}")
    lines.append(">")
    lines.append("> 这个文件由「英语笔记生成器」自动生成，可以随时删；")
    lines.append("> 删了也不会丢收藏 —— 收藏记录在同一个文件夹的 `生词本.json` 里。")
    lines.append("")

    if not entries:
        lines.append("（还没有收藏任何词）")
        lines.append("")
        return "\n".join(lines)

    for index, entry in enumerate(entries, 1):
        head = f"## {index}. {entry.word}"
        if entry.phonetic:
            head += f"　`{entry.phonetic}`"
        lines.append(head)
        lines.append("")
        if entry.pos or entry.meaning:
            lines.append(f"- 释义：{entry.pos} {entry.meaning}".rstrip())
        for sentence in entry.sentences:
            lines.append(f"- 原句：{sentence}")
        if entry.note:
            lines.append(f"- 备注：{entry.note}")
        meta = []
        if entry.tags:
            meta.append("标签：" + "、".join(entry.tags))
        if entry.added_at:
            meta.append(f"收藏于 {entry.added_at}")
        if meta:
            lines.append("- " + "　".join(meta))
        lines.append("")
    return "\n".join(lines)


def render_anki_csv(entries: Sequence[VocabEntry], tag: str = ANKI_TAG) -> str:
    """
    Anki 可导入的 CSV：三列 = 正面 / 背面 / 标签。
    用 csv 模块处理引号和逗号，不会因为释义里有逗号而冲散列。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(["Front", "Back", "Tags"])
    for entry in entries:
        back: List[str] = []
        if entry.phonetic:
            back.append(entry.phonetic)
        if entry.pos or entry.meaning:
            back.append(f"{entry.pos} {entry.meaning}".strip())
        if entry.sentences:
            back.append("例：" + entry.sentences[0])
        writer.writerow([entry.word, "<br>".join(back), tag])
    return buffer.getvalue()


# ============================================================ 自检


def self_test() -> None:
    import shutil
    import tempfile

    print("vocab_book 自检")

    # 临时目录：系统临时目录不可写时（受限环境）退回脚本所在文件夹
    def _temp_dir() -> str:
        try:
            path = tempfile.mkdtemp(prefix="vocabbook_")
            with open(os.path.join(path, "__probe__"), "w", encoding="utf-8") as handle:
                handle.write("ok")
            return path
        except OSError:
            path = os.path.join(BASE_DIR, "__vocabbook_selftest__")
            os.makedirs(path, exist_ok=True)
            return path

    # ---- 数据结构与合并 ----
    a = VocabEntry(word="Ubiquitous", phonetic="/juːˈbɪkwɪtəs/", pos="adj.",
                   meaning="无处不在的", sentences=["Sentence one."], tags=["雅思"])
    b = VocabEntry(word="ubiquitous", meaning="普遍存在的",
                   sentences=["Sentence one.", "Sentence two.", "Sentence three."])
    merged = merge_entry(a, b, stamp="2026-10-11 11:03:15")
    assert merged.word == "Ubiquitous"
    assert merged.meaning == "无处不在的", "已有释义不该被覆盖"
    assert merged.phonetic == "/juːˈbɪkwɪtəs/"
    # 原句去重 + 最多两句
    assert merged.sentences == ["Sentence one.", "Sentence two."], merged.sentences
    assert merged.tags == ["雅思"]
    assert merged.updated_at == "2026-10-11 11:03:15"
    assert merged.key == "ubiquitous", "去重键一律小写"
    assert merged.added_date == "2026-10-11"

    # ---- 收藏 / 去重 / 落盘 ----
    tmp = _temp_dir()
    try:
        folder = os.path.join(tmp, FOLDER_NAME)
        store = Store.load(folder)
        assert len(store) == 0 and not os.path.exists(folder), "第一次用不该凭空建文件夹"

        store.add(a, folder=folder)
        assert len(store) == 1 and store.has("UBIQUITOUS"), "收藏后应能按大小写不敏感命中"
        assert os.path.exists(os.path.join(folder, "生词本.json")), "收藏就该建好文件夹和 JSON"
        assert os.path.exists(os.path.join(folder, "生词本.md")), "同时自动写一份可读 Markdown"

        # 同一个词再收藏一次：不重复，只补齐
        again = store.add(VocabEntry(word="ubiquitous", pos="adj.", meaning="",
                                     sentences=["Sentence two."]), folder=folder)
        assert len(store) == 1, "重复收藏不能变成两条"
        assert again.meaning == "无处不在的"
        assert again.sentences == ["Sentence one.", "Sentence two."]

        # 读回来要一模一样（含中文和音标）
        reloaded = Store.load(folder)
        assert len(reloaded) == 1
        got = reloaded.get("ubiquitous")
        assert got is not None and got.phonetic == "/juːˈbɪkwɪtəs/" and got.tags == ["雅思"]
        assert got.meaning == "无处不在的" and len(got.sentences) == 2
        assert "ubuntu" not in reloaded.words()

        # ---- 两种导出 ----
        md = render_markdown(reloaded.entries)
        assert md.startswith(MARKDOWN_TITLE)
        assert "ubiquitous" in md.lower() and "/juːˈbɪkwɪtəs/" in md
        assert "原句：Sentence one." in md and "标签：雅思" in md

        csv_text = render_anki_csv(reloaded.entries)
        assert csv_text.startswith('"Front","Back","Tags"'), csv_text[:40]
        assert '"Ubiquitous"' in csv_text and "无处不在的" in csv_text
        # 释义里带逗号 / 引号时列不能被冲散
        tricky = VocabEntry(word="comma", meaning='a, b "c"', sentences=["He said, \"hi\"."])
        rows = list(csv.reader(io.StringIO(render_anki_csv([tricky]))))
        assert len(rows[1]) == 3, f"CSV 应恒为三列，实际 {len(rows[1])}"

        # ---- 从磁盘上的 Markdown 恢复不了也没关系，但 JSON 必须能兜住手改 ----
        bogus = os.path.join(tmp, "bogus")
        os.makedirs(bogus)
        with open(os.path.join(bogus, "生词本.json"), "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        assert len(Store.load(bogus)) == 0, "坏文件不能让程序崩，只能当空生词本"
        with open(os.path.join(bogus, "生词本.json"), "w", encoding="utf-8") as handle:
            json.dump({"words": [{"word": "   "}, "乱入的字符串", {"word": "Resilient"},
                                 {"word": "resilient", "meaning": "有韧性的"}]}, handle)
        fixed = Store.load(bogus)
        assert len(fixed) == 1 and fixed.has("resilient"), "脏数据要去重、要跳过空词"

        # ---- 删除 / 清空 / 导出路径 ----
        assert reloaded.remove("Ubiquitous", folder=folder) is True
        assert len(reloaded) == 0 and not reloaded.remove("ubiquitous", folder=folder)
        assert "（还没有收藏任何词）" in render_markdown([])

        path = timestamped_path(".md", folder=folder)
        assert path.startswith(folder) and path.endswith(".md")
        assert FILE_PREFIX in os.path.basename(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"  生词本文件夹名：{FOLDER_NAME}")
    print(f"  数据文件：{os.path.relpath(DATA_FILE, BASE_DIR)}")
    print("vocab_book 自检通过。")


if __name__ == "__main__":
    self_test()
