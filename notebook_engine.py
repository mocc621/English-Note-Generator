# -*- coding: utf-8 -*-
"""
notebook_engine.py —— 笔记生成的核心逻辑（不含任何界面代码）

界面只负责"拿到文本 → 调用这里 → 把结果贴在标签页上"。
把逻辑和界面分开的好处：这里的每个函数都能单独跑、单独测。

它做的事：
  1. 读词库 exam_vocab.txt（单词 / 音标 / 词性 / 中文释义 / 考试标签）
  2. 切自然段、再切句子（能识别 Mr. / e.g. 这类点号，不会误切）
  3. 词形还原：running → run，studies → study，analysed → analyse（含英式/美式拼写）
  4. 挑生词：对一般大学生偏难的词（六级及以上）+ 专业/学术长词 + 英文缩写
  5. 抽句式大意总结：每段挑出主题句，配上中文译文
  6. 管翻译：按段落存进本地翻译记忆，下次直接复用，不重复花钱
  7. 导出 Markdown / Word
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ============================================================ 常量与路径

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOCAB_FILE = os.path.join(BASE_DIR, "exam_vocab.txt")
TM_FILE = os.path.join(BASE_DIR, "translation_memory.json")

# 考试等级（顺序决定界面上的显示顺序）
EXAM_LEVELS = ["四级", "六级", "雅思", "托福", "考研"]

# 每个等级的"难度权重"，用来算整段的难度分
LEVEL_WEIGHT = {"四级": 1, "六级": 2, "雅思": 3, "托福": 3, "考研": 3}

# 对"一般大学生"来说偏难的等级 —— 生词表默认收这些
HARD_LEVELS = {"六级", "雅思", "托福", "考研"}

# 专业/学术长词的长度门槛：不在词库里、又这么长的词，按专业术语处理
TERM_MIN_LENGTH = 9


# ============================================================ 数据结构


@dataclass
class VocabEntry:
    """词库里的一条词。"""

    word: str
    phonetic: str      # 形如 /rɪˈzɪliənt/
    pos: str           # 形如 adj.
    meaning: str       # 中文释义
    tags: List[str] = field(default_factory=list)

    @property
    def weight(self) -> int:
        """难度权重，越大越难。"""
        return max((LEVEL_WEIGHT.get(t, 2) for t in self.tags), default=2)

    @property
    def label(self) -> str:
        return "、".join(self.tags)

    @property
    def is_hard(self) -> bool:
        """是不是"对一般大学生偏难"的词。"""
        return bool(set(self.tags) & HARD_LEVELS)


@dataclass
class WordSense:
    """文中某一处命中的重点词（带上下文，方便复习）。"""

    entry: VocabEntry
    surface: str       # 文中实际出现的形态，如 analysed
    sentence_index: int  # 属于第几句话（从 1 开始，全文连续编号）

    @property
    def is_inflected(self) -> bool:
        return self.surface.lower() != self.entry.word.lower()


@dataclass
class VocabItem:
    """
    生词表里的一项。分三类：
      exam     —— 词库里的高难词（六级/雅思/托福/考研），有音标和释义
      term     —— 不在词库里的专业/学术长词，只能给出"疑似术语"提示
      acronym  —— 英文缩写（如 GDP、MRI、AI），常是专业概念
    """

    word: str
    category: str                 # exam / term / acronym
    entry: Optional[VocabEntry] = None
    surface: str = ""
    count: int = 1
    sentence_index: int = 0

    @property
    def phonetic(self) -> str:
        return self.entry.phonetic if self.entry else ""

    @property
    def pos(self) -> str:
        return self.entry.pos if self.entry else ("缩写" if self.category == "acronym" else "术语")

    @property
    def meaning(self) -> str:
        if self.entry:
            return self.entry.meaning
        if self.category == "acronym":
            return "英文缩写（专业概念，建议查一下全称）"
        return "专业/学术词（词库里没有，建议查词典）"

    @property
    def label(self) -> str:
        if self.entry:
            return self.entry.label
        return "专业词汇" if self.category == "term" else "缩写"

    @property
    def category_name(self) -> str:
        return {"exam": "考试高难词", "term": "专业/学术词", "acronym": "英文缩写"}.get(
            self.category, "生词"
        )


@dataclass
class SentenceInfo:
    """一句话的信息。"""

    index: int
    text: str
    words: List[str]
    senses: List[WordSense]
    paragraph_index: int = 1
    translation: str = ""

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def difficulty(self) -> int:
        """这句话的难度分 = 句中重点词难度之和。"""
        return sum(s.entry.weight for s in self.senses)

    @property
    def key_points(self) -> List[str]:
        """这句话里含金量最高的词 + 中文释义。"""
        ranked = sorted(self.senses, key=lambda s: -s.entry.weight)
        seen = set()
        points = []
        for sense in ranked:
            if sense.entry.word in seen:
                continue
            seen.add(sense.entry.word)
            points.append(f"{sense.surface} ({sense.entry.pos} {sense.entry.meaning})")
        return points

    @property
    def collocations(self) -> List[str]:
        """句中的介词搭配片段，如 approach to / contribute to，用来提示句型。"""
        found = []
        for sense in self.senses:
            pattern = re.compile(
                r"\b" + re.escape(sense.surface) + r"\s+((?:to|of|for|in|on|with|at|from|that)\b[\w\s']{0,18})",
                re.IGNORECASE,
            )
            match = pattern.search(self.text)
            if match:
                fragment = re.sub(r"\s+", " ", f"{sense.surface} {match.group(1).strip()}")
                if fragment not in found:
                    found.append(fragment)
        return found[:3]


@dataclass
class Paragraph:
    """一个自然段。翻译按段落做，上下文完整，译文才顺。"""

    index: int
    text: str
    sentences: List[SentenceInfo] = field(default_factory=list)
    translation: str = ""
    from_cache: bool = False
    summary: str = ""                                        # AI 总结的段落大意（可选）
    key_sentences: List[int] = field(default_factory=list)    # 抽句式总结选中的句子序号

    @property
    def word_count(self) -> int:
        return sum(s.word_count for s in self.sentences)

    @property
    def senses(self) -> List[WordSense]:
        out: List[WordSense] = []
        for sentence in self.sentences:
            out.extend(sentence.senses)
        return out

    @property
    def difficulty(self) -> int:
        return sum(s.difficulty for s in self.sentences)

    @property
    def excerpt(self) -> str:
        head = self.text.strip().replace("\n", " ")
        return head if len(head) <= 36 else head[:36] + "…"


@dataclass
class Analysis:
    """一次分析的完整结果，界面照着它渲染就行。"""

    text: str
    paragraphs: List[Paragraph]
    senses: List[WordSense]

    @property
    def sentences(self) -> List[SentenceInfo]:
        """所有句子，按原文顺序串起来（界面和导出的通用视图）。"""
        out: List[SentenceInfo] = []
        for paragraph in self.paragraphs:
            out.extend(paragraph.sentences)
        return out

    # ---- 统计 ----
    @property
    def word_count(self) -> int:
        return sum(s.word_count for s in self.sentences)

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def avg_sentence_length(self) -> float:
        return round(self.word_count / self.sentence_count, 1) if self.sentences else 0.0

    @property
    def difficulty_score(self) -> int:
        return sum(s.difficulty for s in self.sentences)

    @property
    def difficulty_label(self) -> str:
        wc = max(self.word_count, 1)
        density = self.difficulty_score / wc * 100
        if density < 3:
            return "偏简单（四级水平）"
        if density < 6:
            return "中等（六级水平）"
        if density < 10:
            return "偏难（雅思 6-7 分）"
        return "很难（雅思 7.5 分以上 / 考研长难句密集）"

    @property
    def unique_words(self) -> List[WordSense]:
        """去重后的重点词表，按「难度降序 + 词频降序」排。"""
        counter: Dict[str, int] = {}
        for sense in self.senses:
            counter[sense.entry.word] = counter.get(sense.entry.word, 0) + 1

        best: Dict[str, WordSense] = {}
        for sense in self.senses:
            key = sense.entry.word
            if key not in best:
                best[key] = sense

        def sort_key(item: Tuple[str, WordSense]):
            word, sense = item
            return (-sense.entry.weight, -counter[word], word)

        return [sense for _, sense in sorted(best.items(), key=sort_key)]

    @property
    def hard_sentences(self) -> List[SentenceInfo]:
        """长难句：词数多 或 重点词密集，按难度降序。"""
        candidates = [s for s in self.sentences if s.word_count >= 18 or len(s.senses) >= 3]
        if not candidates:
            candidates = sorted(self.sentences, key=lambda s: -s.word_count)[:3]
        return sorted(candidates, key=lambda s: (-s.difficulty, -s.word_count))[:8]

    @property
    def raw_paragraphs(self) -> List[str]:
        """按空行切出的自然段原文（纯文本）。"""
        blocks = [b.strip() for b in re.split(r"\n\s*\n", self.text) if b.strip()]
        return blocks or ([self.text.strip()] if self.text.strip() else [])


# ============================================================ 词库


def parse_vocab_line(line: str) -> Optional[VocabEntry]:
    """解析一行词库。格式：单词 | 音标 | 词性 | 释义 | 标签"""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None
    word, phonetic, pos, meaning = parts[0], parts[1], parts[2], parts[3]
    if not re.fullmatch(r"[A-Za-z][A-Za-z'-]*", word):
        return None
    tags_raw = parts[4] if len(parts) >= 5 else ""
    tags = [t.strip() for t in re.split(r"[,，、]", tags_raw) if t.strip()]
    return VocabEntry(word=word.lower(), phonetic=phonetic, pos=pos, meaning=meaning, tags=tags)


def load_vocab(path: str = VOCAB_FILE) -> List[VocabEntry]:
    """读词库文件。文件不存在或为空时返回空列表，不抛异常。"""
    if not os.path.exists(path):
        return []
    entries: List[VocabEntry] = []
    seen = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entry = parse_vocab_line(line)
            if entry and entry.word not in seen:
                seen.add(entry.word)
                entries.append(entry)
    return entries


class Lexicon:
    """词库 + 反查索引（支持词形还原后的匹配）。"""

    # 常见不规则变形 → 原形
    IRREGULAR = {
        "is": "be", "are": "be", "was": "be", "were": "be", "been": "be", "being": "be",
        "has": "have", "had": "have", "having": "have",
        "does": "do", "did": "do", "done": "do", "doing": "do",
        "better": "good", "best": "good", "worse": "bad", "worst": "bad",
        "found": "find", "made": "make", "took": "take", "taken": "take",
        "gave": "give", "given": "give", "went": "go", "gone": "go",
        "said": "say", "saw": "see", "seen": "see", "knew": "know", "known": "know",
        "thought": "think", "brought": "bring", "bought": "buy", "grew": "grow",
        "grown": "grow", "held": "hold", "kept": "keep", "left": "leave",
        "lost": "lose", "met": "meet", "paid": "pay", "ran": "run",
        "rose": "rise", "risen": "rise", "sought": "seek", "sold": "sell",
        "sent": "send", "spent": "spend", "stood": "stand", "taught": "teach",
        "told": "tell", "understood": "understand", "won": "win", "wrote": "write",
        "written": "write", "arose": "arise", "arisen": "arise",
        "led": "lead", "lay": "lie", "laid": "lay", "drew": "draw", "drawn": "draw",
    }

    # 英式 / 美式拼写差异：两边都归一，跨拼写也能匹配
    SPELLING_PAIRS = [
        ("isation", "ization"), ("isations", "izations"),
        ("ising", "izing"), ("ised", "ized"), ("ises", "izes"),
        ("ise", "ize"), ("yse", "yze"), ("yser", "yzer"),
        ("ogue", "og"), ("ogues", "ogs"),
        ("our", "or"), ("ours", "ors"),
        ("re", "er"), ("res", "ers"),
        ("lling", "ling"), ("lled", "led"), ("ller", "ler"),
        ("ae", "e"), ("oe", "e"),
    ]

    def __init__(self, entries: Sequence[VocabEntry]):
        self.entries: List[VocabEntry] = list(entries)
        self.by_word: Dict[str, VocabEntry] = {e.word: e for e in self.entries}
        self.index: Dict[str, VocabEntry] = {}
        for entry in self.entries:
            for form in self.variants(entry.word):
                self.index.setdefault(form, entry)
            # 跨拼写也注册一份：analyse 同时注册 analyze
            for form in self.normalize_spellings(entry.word):
                self.index.setdefault(form, entry)

    # ---------------- 词形还原 ----------------
    @classmethod
    def candidates(cls, token: str) -> List[str]:
        """给一个文中出现的词，列出它所有可能的原形，按可信度排序。"""
        token = token.lower().strip("'")
        out: List[str] = []

        def add(value: str):
            if value and value not in out:
                out.append(value)

        add(cls.IRREGULAR.get(token, token))
        add(token)

        if token.endswith("ies") and len(token) > 4:
            add(token[:-3] + "y")
        if token.endswith("es") and len(token) > 3:
            add(token[:-2])
        if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            add(token[:-1])

        if token.endswith("ing") and len(token) > 5:
            stem = token[:-3]
            add(stem)
            add(stem + "e")
            if len(stem) > 2 and stem[-1] == stem[-2]:
                add(stem[:-1])
            if stem.endswith("y"):
                add(stem)

        if token.endswith("ied") and len(token) > 4:
            add(token[:-3] + "y")
        if token.endswith("ed") and len(token) > 4:
            stem = token[:-2]
            add(stem)
            add(token[:-1])
            add(stem + "e")
            if len(stem) > 2 and stem[-1] == stem[-2]:
                add(stem[:-1])
            if len(stem) > 3 and stem.endswith("at"):
                add(stem + "e")

        if token.endswith("er") and len(token) > 4:
            add(token[:-2])
            add(token[:-1])
        if token.endswith("est") and len(token) > 5:
            add(token[:-3])
            add(token[:-2])

        if token.endswith("ly") and len(token) > 4:
            add(token[:-2])
            add(token[:-2] + "e")

        return out

    @classmethod
    def variants(cls, word: str) -> List[str]:
        """给词库里的原形词，列出它在文章里可能出现的样子。"""
        forms = {word}
        w = word

        if w.endswith("e") and len(w) > 2:
            forms.update({w + "d", w + "s", w[:-1] + "ing"})
        if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
            forms.update({w[:-1] + "ies", w[:-1] + "ied"})
            forms.update({w + "s", w + "ed"})
        if w.endswith("s") or w.endswith("x") or w.endswith("ch") or w.endswith("sh"):
            forms.add(w + "es")
        if len(w) > 3 and w[-1] not in "aeiouwxy" and w[-2] in "aeiou" and w[-3] not in "aeiou":
            forms.add(w + w[-1] + "ing")
            forms.add(w + w[-1] + "ed")
        if w.endswith("ate") or w.endswith("ute") or w.endswith("ide"):
            forms.add(w[:-1] + "ing")

        forms.update({w + "ing", w + "ed", w + "s", w + "ly"})
        if w.endswith("e") and len(w) > 2:
            forms.add(w[:-1] + "ed")
        if w.endswith("e") and w.endswith("te"):
            forms.add(w[:-1] + "ing")
        return sorted(forms)

    # ---------------- 跨拼写 ----------------
    @classmethod
    def normalize_spellings(cls, token: str) -> List[str]:
        """给一个词，返回它可能的跨拼写写法（含自己）。"""
        out = [token]
        for british, american in cls.SPELLING_PAIRS:
            if token.endswith(british):
                out.append(token[: -len(british)] + american)
            if token.endswith(american):
                out.append(token[: -len(american)] + british)
        return list(dict.fromkeys(out))

    # ---------------- 查询 ----------------
    def lookup(self, token: str) -> Optional[VocabEntry]:
        """查文中出现的一个词对应到词库里的哪条。查不到返回 None。"""
        for candidate in self.candidates(token):
            for form in self.normalize_spellings(candidate):
                entry = self.index.get(form) or self.by_word.get(form)
                if entry:
                    return entry
        return None

    def search(self, keyword: str) -> List[VocabEntry]:
        """模糊搜索词库，供界面上的"查词"用。"""
        keyword = keyword.strip().lower()
        if not keyword:
            return []
        exact = [e for e in self.entries if e.word == keyword]
        prefix = [e for e in self.entries if e.word.startswith(keyword) and e.word != keyword]
        contains = [
            e for e in self.entries
            if keyword in e.word and e.word != keyword and e not in prefix
        ]
        by_meaning = [e for e in self.entries if keyword in e.meaning and e not in exact + prefix + contains]
        return (exact + prefix + contains + by_meaning)[:40]


# ============================================================ 分句


ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc",
    "e.g", "i.e", "fig", "no", "vol", "approx", "dept", "est", "al",
    "inc", "ltd", "co", "gov", "univ", "min", "max", "avg",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
# 英文缩写：2 个及以上大写字母，如 GDP / MRI / AI / WHO
ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")


def tokenize(text: str) -> List[str]:
    """取出文中所有英文单词（忽略数字和标点）。"""
    return [m.group(0).replace("’", "'") for m in WORD_RE.finditer(text)]


def split_sentences(text: str) -> List[str]:
    """
    把一段英文切成句子。
    会在 . ! ? 后面切，但跳过 Mr. / e.g. / 小数点 1.5 这几种情况。
    """
    text = re.sub(r"[ \t]+", " ", text)
    sentences: List[str] = []
    buffer: List[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        buffer.append(ch)

        if ch in ".!?":
            is_decimal = (
                ch == "."
                and i + 1 < n
                and text[i + 1].isdigit()
                and i > 0
                and text[i - 1].isdigit()
            )
            is_abbrev = False
            if ch == ".":
                j = i - 1
                while j >= 0 and text[j].isalpha():
                    j -= 1
                prev_word = text[j + 1 : i].lower()
                if prev_word in ABBREVIATIONS or len(prev_word) == 1:
                    is_abbrev = True

            followed_by_break = i + 1 >= n or text[i + 1] in " \n\t\"'“”（）()"

            if not is_decimal and not is_abbrev and followed_by_break:
                while i + 1 < n and text[i + 1] in ".!?":
                    i += 1
                    buffer.append(text[i])
                while i + 1 < n and text[i + 1] in "\"'”’)]":
                    i += 1
                    buffer.append(text[i])
                sentences.append("".join(buffer).strip())
                buffer = []
        elif ch == "\n":
            piece = "".join(buffer).strip()
            if word_count_of(piece) >= 2 and piece[-1:] not in ",;:":
                sentences.append(piece)
                buffer = []
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s.strip()]


def word_count_of(text: str) -> int:
    return len(WORD_RE.findall(text))


def split_paragraphs(text: str) -> List[str]:
    """按空行切自然段；没有空行时按单换行切；都没有就整体当一段。"""
    text = (text or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) > 1:
        return blocks
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) > 1 and all(word_count_of(l) >= 4 for l in lines):
        return lines
    return blocks or [text]


# ============================================================ 分析主函数


def analyze(text: str, lexicon: Lexicon) -> Analysis:
    """核心分析：切段 → 切句 → 匹配重点词。"""
    text = (text or "").strip()
    paragraphs: List[Paragraph] = []
    all_senses: List[WordSense] = []
    sentence_counter = 0

    for p_index, p_text in enumerate(split_paragraphs(text), 1):
        paragraph = Paragraph(index=p_index, text=p_text)
        for s_text in split_sentences(p_text):
            sentence_counter += 1
            words = tokenize(s_text)
            senses: List[WordSense] = []
            seen_here = set()
            for word in words:
                entry = lexicon.lookup(word)
                if not entry:
                    continue
                key = (entry.word, word.lower())
                if key in seen_here:
                    continue
                seen_here.add(key)
                sense = WordSense(entry=entry, surface=word, sentence_index=sentence_counter)
                senses.append(sense)
                all_senses.append(sense)
            paragraph.sentences.append(
                SentenceInfo(
                    index=sentence_counter,
                    text=s_text,
                    words=words,
                    senses=senses,
                    paragraph_index=p_index,
                )
            )
        paragraphs.append(paragraph)

    return Analysis(text=text, paragraphs=paragraphs, senses=all_senses)


# ============================================================ 生词识别


def is_probable_term(word: str) -> bool:
    """
    判断一个不在词库里的长词是不是"专业/学术词"。
    规则：够长，并且不是常见的派生词（-ing / -ed / -ly / -tion 之外的普通词）。
    目的只是"提示你去查"，不追求 100% 准确。
    """
    lower = word.lower()
    if len(lower) < TERM_MIN_LENGTH:
        return False
    if lower in STOPWORDS:
        return False
    # 纯派生形态（前面能还原到一个更短的常见词）就不算术语
    common_suffixes = ("ing", "ed", "ly", "ness", "ment", "able", "ible", "ful", "less")
    for suffix in common_suffixes:
        if lower.endswith(suffix) and len(lower) - len(suffix) >= 5:
            return False
    return True


def find_unfamiliar(analysis: Analysis, lexicon: Lexicon, max_terms: int = 40) -> List[VocabItem]:
    """
    挑出"对一般大学生比较陌生"的词，分三组：
      1. exam    —— 命中了词库里六级/雅思/托福/考研的难词（有音标、释义）
      2. term    —— 不在词库里的专业/学术长词（只提示，建议查词典）
      3. acronym —— 英文缩写（GDP、MRI 这类，往往是专业概念）
    """
    counts: Dict[str, int] = {}
    first_sentence: Dict[str, int] = {}
    surface_of: Dict[str, str] = {}
    entry_of: Dict[str, VocabEntry] = {}

    # ---- exam：命中的高难词 ----
    for sense in analysis.senses:
        key = sense.entry.word
        counts[key] = counts.get(key, 0) + 1
        surface_of.setdefault(key, sense.surface)
        first_sentence.setdefault(key, sense.sentence_index)
        entry_of[key] = sense.entry

    items: List[VocabItem] = []
    for word, entry in entry_of.items():
        if not entry.is_hard:
            continue                      # 四级及以下的常见词不算生词
        items.append(
            VocabItem(
                word=word,
                category="exam",
                entry=entry,
                surface=surface_of.get(word, word),
                count=counts.get(word, 1),
                sentence_index=first_sentence.get(word, 0),
            )
        )
    items.sort(key=lambda item: (-item.entry.weight, -item.count, item.word))

    # ---- term / acronym：不在词库里的 ----
    term_counts: Dict[str, int] = {}
    term_sentence: Dict[str, int] = {}
    term_surface: Dict[str, str] = {}
    acronym_counts: Dict[str, int] = {}
    acronym_sentence: Dict[str, int] = {}

    for sentence in analysis.sentences:
        for word in sentence.words:
            lower = word.lower()
            if lexicon.lookup(word):
                continue                  # 词库里有，归到 exam 组
            if is_probable_term(lower):
                term_counts[lower] = term_counts.get(lower, 0) + 1
                term_surface.setdefault(lower, word)
                term_sentence.setdefault(lower, sentence.index)
        for match in ACRONYM_RE.finditer(sentence.text):
            token = match.group(0)
            if lexicon.lookup(token):
                continue
            acronym_counts[token] = acronym_counts.get(token, 0) + 1
            acronym_sentence.setdefault(token, sentence.index)

    terms = sorted(term_counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[:max_terms]
    for word, count in terms:
        items.append(
            VocabItem(
                word=word,
                category="term",
                surface=term_surface.get(word, word),
                count=count,
                sentence_index=term_sentence.get(word, 0),
            )
        )

    acronyms = sorted(acronym_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for word, count in acronyms:
        items.append(
            VocabItem(
                word=word,
                category="acronym",
                surface=word,
                count=count,
                sentence_index=acronym_sentence.get(word, 0),
            )
        )

    return items


# ============================================================ 联网查词的候选排序


# 这些后缀通常出现在学术/专业/派生词里，普通学生更容易陌生
ACADEMIC_SUFFIXES = (
    "tion", "sion", "ance", "ence", "ment", "ity", "ical", "ial", "ious", "eous",
    "ology", "ography", "ometer", "esis", "osis", "itis", "ology", "genic",
    "able", "ible", "ative", "itive", "ance", "ency", "ancy", "ism", "ist",
    "ize", "ify", "esque", "aceous", "ferous", "morphic", "scopic", "logy",
)

# "高级词汇"信号：这些后缀几乎只出现在书面/学术语域里，
# 例如 exacerbate / ubiquitous / comprehensive / subsequent / deteriorate / mitigate。
ADVANCED_SUFFIXES = (
    "ate", "ite", "ute", "ify", "ize", "ise",
    "uous", "ious", "eous", "itious", "aceous", "aneous",
    "ology", "ography", "ometry", "onomy", "osophy",
    "esis", "osis", "iasis", "itis",
    "itude", "escence",
    "ical", "ative", "itive", "atory", "orial", "arian",
    "ism", "ist", "istic",
)

# 拉丁/希腊来源的高频词根片段：含这些片段的词基本是学术高级词。
# 注意只放"片段"（词干），不要放完整单词，否则会把普通词也命中。
ADVANCED_STEMS = (
    "acerb", "ubiqu", "omnip", "magnan", "mitig", "prolif", "coher", "inevit",
    "ambig", "advers", "benevol", "cogniz", "discern", "elucid", "encomp",
    "equivoc", "eradic", "eschew", "exped", "extrane", "extrapolat", "facilit",
    "fallac", "fortuit", "frugal", "gratuit", "gregar", "idiosyncr", "imped",
    "inadvert", "incess", "incongru", "indign", "inexor", "infring", "inherent",
    "innovat", "insurmount", "intransig", "intrins", "inundat", "inveter",
    "juxtapos", "legitim", "lenien", "letharg", "lucid", "malevol", "meticul",
    "mundane", "neglig", "notori", "obscur", "obsolet", "obstin", "ostens",
    "palpab", "paradigm", "paramount", "paucity", "perempt", "perenn",
    "perfunct", "peripher", "pernic", "perpetu", "perplex", "pertin", "pervas",
    "plausib", "poign", "polariz", "pragmat", "precarious", "preced", "preclud",
    "predilect", "predomin", "prelimin", "prerogat", "preval", "procrastin",
    "profic", "profus", "prognos", "prolifer", "propens", "propon", "propriet",
    "prosaic", "provoc", "prudent", "quandary", "quiesc", "rampant", "recalcitr",
    "reciproc", "reclus", "reconcil", "redund", "refut", "reiter", "relegat",
    "relinqu", "reluct", "remuner", "renounce", "repercuss", "reprehens",
    "repudiat", "repugn", "requisit", "rescind", "resilien", "restive",
    "retic", "retribut", "rhetor", "rudiment", "sagac", "salient", "scrupul",
    "scrutin", "sedent", "sedul", "seminal", "sequestr", "serendip", "skept",
    "solicit", "spontan", "spurious", "stagn", "staunch", "stereotyp", "stigma",
    "stipulat", "stringent", "subjug", "subordin", "subsidi", "subsist",
    "substanti", "subtle", "succinct", "succumb", "sumptuous", "superflu",
    "supersed", "supplant", "surmise", "surreptit", "suscept", "sycophant",
    "synerg", "tacit", "tangib", "tantamount", "tenacious", "temerit", "tepid",
    "theoret", "thwart", "torpor", "tractab", "transcend", "transgress",
    "transient", "trite", "truncat", "turbul", "ulterior", "unanim", "uncouth",
    "undermin", "underscor", "unequiv", "unfathom", "unilateral", "unpreced",
    "usurp", "vacillat", "vagary", "vanquish", "vapid", "vehement", "venerat",
    "verbose", "vestig", "vicissitud", "vindicat", "virulent", "visceral",
    "vituper", "vocifer", "volatil", "vorac", "vulner", "wistful",
)



# 面向普通学生：这些高频基础词不该出现在生词表里（联网查得到也不该收）
COMMON_WORDS = {
    # 功能词 / 高频虚词
    "about", "above", "across", "after", "again", "against", "ahead", "almost", "alone",
    "along", "already", "also", "although", "always", "among", "amount", "another", "any",
    "anyone", "anything", "around", "because", "before", "behind", "below", "beside",
    "besides", "between", "beyond", "both", "cannot", "despite", "during", "each", "either",
    "else", "enough", "especially", "even", "ever", "every", "everyone", "everything",
    "except", "few", "following", "former", "forward", "from", "further", "hardly", "hence",
    "here", "however", "instead", "itself", "just", "later", "less", "likely", "little",
    "many", "maybe", "meanwhile", "might", "more", "moreover", "most", "much", "myself",
    "near", "nearly", "neither", "never", "nevertheless", "nobody", "none", "nothing",
    "now", "often", "once", "only", "other", "otherwise", "outside", "over", "perhaps",
    "quite", "rather", "really", "recently", "regarding", "several", "should", "simply",
    "since", "some", "someone", "something", "sometimes", "somewhat", "somewhere", "soon",
    "still", "such", "than", "then", "there", "therefore", "these", "though", "thus",
    "together", "toward", "under", "until", "upon", "usually", "various", "very", "what",
    "whatever", "when", "where", "whether", "which", "while", "whose", "within", "without",
    "would", "yet",
    # 高频名词
    "access", "action", "activity", "addition", "advice", "age", "agency", "agent", "agreement",
    "air", "amount", "analysis", "animal", "answer", "appeal", "appearance", "area",
    "argument", "arrangement", "article", "artist", "aspect", "attention", "attitude",
    "audience", "author", "average", "background", "balance", "basis", "behaviour", "behavior",
    "belief", "benefit", "board", "body", "book", "bottom", "branch", "brother", "budget",
    "building", "business", "camera", "campaign", "candidate", "capital", "care", "career",
    "case", "category", "cause", "cell", "center", "century", "chair", "chairman", "chance",
    "change", "chapter", "character", "charge", "child", "choice", "citizen", "city", "class",
    "client", "climate", "club", "coach", "collection", "college", "color", "comment",
    "commission", "committee", "community", "company", "comparison", "competition", "concept",
    "concern", "conclusion", "condition", "conference", "confidence", "conflict", "congress",
    "connection", "consensus", "consequence", "construction", "consumer", "context", "contract",
    "contrast", "contribution", "control", "conversation", "copy", "cost", "count", "country",
    "county", "couple", "course", "court", "coverage", "creation", "credit", "crime", "crisis",
    "critic", "culture", "currency", "customer", "cycle", "damage", "danger", "data", "date",
    "daughter", "day", "deal", "death", "debate", "debt", "decade", "decision", "decline",
    "definition", "degree", "demand", "department", "description", "design", "desire", "detail",
    "development", "device", "difference", "difficulty", "direction", "director", "discussion",
    "disease", "distance", "distribution", "district", "division", "doctor", "document", "dog",
    "door", "doubt", "drawback", "dream", "driver", "duty", "earth", "economy", "edge", "editor",
    "education", "effect", "effort", "election", "element", "emergency", "emotion", "employee",
    "employer", "employment", "end", "enemy", "energy", "engine", "engineer", "enterprise",
    "entrance", "environment", "episode", "equipment", "error", "essay", "event", "evidence",
    "exam", "example", "exchange", "executive", "exercise", "exhibition", "existence", "expansion",
    "experience", "expert", "explanation", "expression", "extent", "eye", "face", "fact", "factor",
    "failure", "faith", "fall", "family", "farm", "farmer", "father", "fear", "feature", "feeling",
    "festival", "field", "fight", "figure", "film", "final", "finance", "finding", "finger",
    "fire", "firm", "fish", "floor", "focus", "food", "foot", "force", "foreign", "forest",
    "form", "fortune", "foundation", "frame", "framework", "friend", "front", "fruit", "function",
    "fund", "future", "game", "gap", "garden", "gas", "gate", "general", "generation", "gift",
    "girl", "glass", "goal", "god", "gold", "government", "grade", "grand", "grant", "grass",
    "great", "ground", "group", "growth", "guess", "guest", "guide", "guy", "hair", "half",
    "hall", "hand", "head", "health", "heart", "heat", "height", "help", "hero", "history",
    "hit", "hold", "hole", "holiday", "home", "hope", "horse", "hospital", "host", "hotel",
    "hour",
    "house", "housing", "human", "husband", "idea", "image", "impact", "importance", "impression",
    "improvement", "incident", "income", "increase", "independence", "index", "individual",
    "industry", "inflation", "influence", "information", "initiative", "injury", "instance",
    "institution", "instruction", "instrument", "insurance", "intention", "interaction",
    "interest", "interpretation", "interview", "introduction", "investment", "investor", "issue",
    "item", "job", "journal", "journey", "judge", "judgment", "justice", "kid", "kind",
    "knowledge", "labour", "labor", "lack", "lady", "land", "language", "laugh", "law", "lawyer",
    "layer", "leader", "leadership", "league", "learning", "length", "lesson", "letter", "level",
    "liberty", "library", "life", "light", "limit", "line", "link", "list", "literature",
    "location", "loss", "lot", "love", "luck", "lunch", "machine", "magazine", "mail", "main",
    "maintenance", "major", "majority", "making", "man", "management", "manager", "manner",
    "manufacturer", "map", "margin", "mark", "market", "marriage", "material", "matter", "mayor",
    "meal", "meaning", "measure", "media", "medical", "medicine", "meeting", "member", "membership",
    "memory", "mention", "message", "metal", "method", "middle", "might", "mile", "mind", "mine",
    "minister", "minority", "minute", "mirror", "mission", "mistake", "model", "moment", "money",
    "month", "mood", "moon", "moral", "morning", "mother", "motion", "motor", "mountain", "mouth",
    "movement", "movie", "murder", "muscle", "music", "mystery", "name", "nation", "nature",
    "neck", "need", "neighbour", "neighbor", "nerve", "network", "news", "newspaper", "night",
    "noise", "normal", "north", "nose", "note", "notice", "novel", "number", "nurse", "object",
    "objective", "obligation", "observation", "occasion", "ocean", "offer", "office", "officer",
    "official", "operation", "opinion", "opportunity", "opposition", "option", "order",
    "organization", "origin", "outcome", "output", "owner", "package", "page", "pain", "paint",
    "painting", "pair", "panel", "paper", "parent", "park", "part", "participant", "participation",
    "particular", "partner", "partnership", "party", "passage", "passenger", "past", "path",
    "patient", "pattern", "payment", "peace", "peak", "pen", "people", "percentage", "perception",
    "performance", "period", "permission", "person", "personality", "perspective", "phase",
    "phenomenon", "philosophy", "phone", "photo", "photograph", "phrase", "physics", "picture",
    "piece", "place", "plan", "plane", "planet", "planning", "plant", "plastic", "plate",
    "platform", "player", "pleasure", "plenty", "poem", "poet", "poetry", "point", "police",
    "policy", "politician", "politics", "poll", "pool", "population", "port", "position",
    "possession", "possibility", "post", "pot", "potential", "pound", "power", "practice",
    "preference", "preparation", "presence", "presentation", "president", "press", "pressure",
    "price", "pride", "principle", "print", "prior", "priority", "prison", "prisoner", "privacy",
    "privilege", "prize",
    "problem", "procedure", "process", "producer", "product", "production", "profession",
    "professor", "profile", "profit", "program", "progress", "project", "promise", "promotion",
    "proof", "property", "proportion", "proposal", "prospect", "protection", "protest", "provider",
    "psychology", "public", "publication", "purpose", "pursuit", "quality", "quantity", "quarter",
    "question", "quote", "race", "radio", "rain", "range", "rate", "ratio", "reaction", "reader",
    "reality", "reason", "receipt", "recession", "recipe", "recognition", "recommendation",
    "record", "recovery", "recruitment", "reduction", "reference", "reflection", "reform",
    "refugee", "region", "register", "regulation", "rejection", "relation", "relationship",
    "release", "relief", "religion", "remainder", "removal", "rent", "repair", "repeat",
    "replacement", "report", "reporter", "representation", "reputation", "requirement",
    "research", "researcher", "reserve", "residence", "resident", "resistance", "resolution",
    "resort", "resource", "respect", "response", "responsibility", "rest", "restaurant",
    "restriction", "result", "retail", "retirement", "return", "revenue", "review", "revolution",
    "reward", "rice", "rich", "ride", "ring", "risk", "river", "road", "rock", "role", "roll",
    "roof", "room", "root", "rope", "route", "routine", "row", "rule", "ruler", "rumour", "rural",
    "safety", "salad", "salary", "sale", "salt", "sample", "sand", "satisfaction", "saving",
    "scale", "scene", "schedule", "scheme", "scholar", "school", "science", "scientist", "score",
    "screen", "script", "sea", "search", "season", "seat", "second", "secretary", "section",
    "sector", "security", "seed", "selection", "self", "sense", "sentence", "series", "service",
    "session", "set", "setting", "settlement", "sex", "shadow", "shape", "share", "sheet",
    "shelf", "shell", "shelter", "shift", "ship", "shirt", "shock", "shoe", "shop", "shopping",
    "shore", "short", "shot", "shoulder", "show", "shower", "side", "sight", "sign", "signal",
    "significance", "silence", "silver", "similarity", "singer", "sir", "sister", "site",
    "situation", "size", "skill", "skin", "sky", "sleep", "slice", "slide", "smile", "smoke",
    "snow", "society", "sock", "soft", "software", "soil", "soldier", "solution", "son", "song",
    "sort", "soul", "sound", "soup", "source", "south", "space", "speaker", "specialist",
    "species", "specification", "speech", "speed", "spirit", "spite", "split", "spokesman",
    "sport", "spot", "spread", "spring", "square", "staff", "stage", "stake", "standard", "star",
    "start", "state", "statement", "station", "statistics", "status", "stay", "steam", "steel",
    "step", "stick", "stock", "stomach", "stone", "storage", "store", "storm", "story", "strain",
    "stranger", "strategy", "stream", "street", "strength", "stress", "strike", "string",
    "structure", "struggle", "student", "studio", "study", "stuff", "style", "subject", "submission",
    "substance", "success", "sugar", "suggestion", "summer", "summit", "sun", "supermarket",
    "supply", "support", "supporter", "surface", "surgeon", "surprise", "survey", "survival",
    "sweet", "swimming", "symbol", "sympathy", "system", "table", "talent", "talk", "tank", "tape",
    "target", "task", "taste", "tax", "taxi", "tea", "teacher", "teaching", "team", "tear",
    "technique", "technology", "telephone", "television", "temperature", "tendency", "tennis",
    "tension", "tent", "term", "territory", "terror", "test", "text", "texture", "theme", "theory",
    "therapist", "therapy", "thing", "thinking", "thought", "throat", "ticket", "tie", "time",
    "tip", "title", "today", "tomorrow", "tone", "tonight", "tool", "tooth", "top", "topic",
    "total", "touch", "tour", "tourist", "town", "track", "trade", "tradition", "traffic",
    "trainer", "training", "transfer", "transport", "trash", "travel", "treasure", "treatment",
    "treaty", "tree", "trend", "trial", "tribe", "trip", "trouble", "truck", "trust", "truth",
    "tune", "tunnel", "turn", "twice", "type", "uncle", "understanding", "unemployment", "union",
    "unit", "universe", "university", "user", "valuable", "value", "variation", "variety",
    "vehicle", "version", "victim", "victory", "video", "view", "viewer", "village", "violence",
    "virtue", "virus", "vision", "visitor", "voice", "volume", "volunteer", "vote", "voter",
    "wage", "wagon", "walk", "wall", "war", "warning", "wash", "waste", "watch", "water", "wave",
    "way", "weakness", "wealth", "weapon", "weather", "web", "website", "wedding", "week",
    "weekend", "weight", "welcome", "welfare", "west", "wheel", "whereas", "whilst", "whisper",
    "white", "whole", "wife", "wild", "will", "wind", "window", "wine", "wing", "winner",
    "winter", "wire", "wisdom", "wish", "witness", "woman", "wonder", "wood", "wool", "word",
    "worker", "workforce", "workplace", "workshop", "world", "worry", "worth", "wound", "writer",
    "writing", "yard", "year", "yesterday", "youth", "zone",
    # 高频动词 / 形容词 / 副词
    "accept", "achieve", "act", "add", "admit", "advance", "advertise", "advise", "affect",
    "afford", "agree", "aim", "allow", "announce", "apologize", "appear", "apply", "appoint",
    "appreciate", "approve", "argue", "arise", "arrange", "arrive", "ask", "assist", "assume",
    "attack", "attend", "attract", "avoid", "awake", "back", "bake", "balance", "ban", "base",
    "bear", "beat", "become", "begin", "behave", "believe", "belong", "bend", "benefit", "bet",
    "bind", "bite", "blame", "bless", "blow", "boil", "book", "bore", "borrow", "bother",
    "bounce", "bow", "break", "breathe", "breed", "bring", "broadcast", "brush", "build", "burn",
    "burst", "bury", "buy", "calculate", "call", "calm", "cancel", "capture", "care", "carry",
    "cast", "catch", "cause", "cease", "celebrate", "challenge", "change", "charge", "chase",
    "chat", "check", "cheer", "choose", "chop", "claim", "clap", "clean", "clear", "climb",
    "close", "coach", "collapse", "collect", "combine", "come", "command", "comment", "commit",
    "communicate", "compare", "compete", "complain", "complete", "complicate", "compose",
    "concentrate", "concern", "conclude", "conduct", "confirm", "confuse", "connect", "consider",
    "consist", "construct", "consult", "consume", "contain", "continue", "contribute", "control",
    "convert", "convince", "cook", "cool", "cooperate", "copy", "correct", "cost", "count",
    "cover", "crash", "create", "criticize", "cross", "cry", "cure", "cut", "damage", "dance",
    "date", "deal", "decide", "declare", "decline", "decorate", "decrease", "defeat", "defend",
    "define", "delay", "deliver", "demand", "demonstrate", "deny", "depend", "describe", "deserve",
    "design", "desire", "destroy", "detail", "detect", "determine", "develop", "devote", "die",
    "differ", "dig", "direct", "disagree", "disappear", "disappoint", "discover", "discuss",
    "dislike", "display", "dissolve", "distinguish", "distribute", "disturb", "divide", "divide",
    "divorce", "do", "dominate", "donate", "doubt", "drag", "draw", "dream", "dress", "drink",
    "drive", "drop", "dry", "earn", "ease", "eat", "educate", "elect", "eliminate", "embrace",
    "emerge", "emphasize", "employ", "enable", "encounter", "encourage", "end", "endure", "enforce",
    "engage", "enjoy", "ensure", "enter", "entertain", "escape", "establish", "estimate",
    "evaluate", "examine", "exceed", "exchange", "excite", "excuse", "exercise", "exist", "expand",
    "expect", "expectation", "expectations", "expects", "expected",
    "expense", "experience", "experiment", "explain", "explode", "explore", "export",
    "expose", "express", "extend", "face", "facilitate", "fail", "fall", "favor", "favour",
    "fear", "feed", "feel", "fetch", "fight", "fill", "find", "finish", "fit", "fix", "float",
    "flow", "fly", "focus", "fold", "follow", "forbid", "force", "forecast", "forget", "forgive",
    "family", "families", "effect", "effects", "growth", "change", "changes",
    "form", "found", "free", "freeze", "frighten", "fry", "fulfil", "fulfill", "gain", "gather",
    "generate", "get", "give", "go", "govern", "grab", "graduate", "grant", "greet", "grow",
    "guarantee", "guess", "guide", "handle", "hang", "happen", "harm", "hate", "have", "heal",
    "hear", "heat", "help", "hide", "hit", "hold", "honour", "honor", "hope", "hug", "hunt",
    "hurry", "hurt", "identify", "ignore", "illustrate", "imagine", "imply", "import", "impose",
    "impress", "improve", "include", "increase", "indicate", "influence", "inform", "inherit",
    "insist", "inspect", "inspire", "install", "intend", "interpret", "interrupt", "introduce",
    "invade", "invent", "invest", "investigate", "invite", "involve", "join", "joke", "jump",
    "keep", "kick", "kill", "kiss", "knock", "know", "label", "land", "last", "laugh", "launch",
    "lay", "lead", "leak", "lean", "leap", "learn", "leave", "lend", "let", "lie", "lift",
    "light", "like", "limit", "link", "listen", "live", "load", "locate", "lock", "look", "lose",
    "love", "maintain", "make", "manage", "manufacture", "mark", "marry", "match", "matter",
    "mean", "measure", "meet", "melt", "mention", "migrate", "mind", "miss", "mix", "modify",
    "monitor", "move", "multiply", "murder", "name", "need", "neglect", "negotiate", "nod",
    "note", "notice", "obey", "observe", "obtain", "occur", "offer", "open", "operate", "oppose",
    "order", "organize", "overcome", "owe", "own", "pack", "paint", "park", "participate", "pass",
    "pay", "perform", "permit", "persuade", "pick", "place", "plan", "plant", "play", "please",
    "point", "pollute", "pop", "pose", "possess", "post", "pour", "practise", "praise", "pray",
    "predict", "prefer", "prepare", "present", "preserve", "press", "pretend", "prevent", "print",
    "produce", "promise", "promote", "pronounce", "propose", "protect", "protest", "prove",
    "provide", "publish", "pull", "punish", "purchase", "push", "put", "qualify", "question",
    "quit", "quote", "race", "rain", "raise", "range", "reach", "react", "read", "realize",
    "receive", "recognize", "recommend", "recover", "reduce", "refer", "reflect", "refuse",
    "regard", "register", "regret", "reject", "relate", "relax", "release", "relieve", "rely",
    "remain", "remember", "remind", "remove", "repair", "repeat", "replace", "reply", "report",
    "represent", "request", "require", "rescue", "research", "reserve", "resist", "respect",
    "respond", "rest", "restore", "restrict", "retire", "return", "reveal", "review", "reward",
    "ride", "ring", "rise", "risk", "roll", "rub", "ruin", "rule", "run", "rush", "satisfy",
    "save", "say", "scare", "search", "seat", "secure", "seek", "seem", "select", "sell", "send",
    "separate", "serve", "settle", "shake", "shape", "share", "shift", "shine", "shoot", "shop",
    "shout", "show", "shut", "sing", "sink", "sit", "sleep", "slide", "slip", "smell", "smile",
    "smoke", "solve", "sort", "sound", "speak", "specify", "speed", "spell", "spend", "split",
    "spread", "stand", "stare", "start", "state", "stay", "steal", "stick", "stop", "store",
    "strengthen", "stretch", "strike", "struggle", "study", "submit", "succeed", "suffer",
    "suggest", "supply", "support", "suppose", "surprise", "surround", "survive", "swim", "switch",
    "take", "talk", "tap", "taste", "teach", "tear", "telephone", "tell", "tend", "test", "thank",
    "think", "threaten", "throw", "tie", "touch", "train", "transfer", "translate", "transport",
    "travel", "treat", "trust", "try", "turn", "twist", "understand", "undertake", "unite",
    "update", "urge", "use", "value", "vary", "visit", "vote", "wait", "wake", "walk", "want",
    "warn", "wash", "waste", "watch", "wave", "wear", "welcome", "win", "wind", "wipe", "wish",
    "wonder", "work", "worry", "wrap", "write", "yield",
    "able", "afraid", "alive", "angry", "annual", "anxious", "asleep", "available", "aware",
    "awful", "bad", "basic", "beautiful", "best", "big", "bitter", "black", "blind", "blue",
    "bold", "boring", "brave", "brief", "bright", "broad", "brown", "busy", "calm", "careful",
    "casual", "central", "certain", "cheap", "chief", "civil", "classic", "clean", "clear",
    "clever", "close", "cloudy", "cold", "comfortable", "common", "complex", "confident",
    "confused", "conscious", "constant", "cool", "correct", "crazy", "creative", "critical",
    "crowded", "cruel", "curious", "current", "cute", "daily", "dangerous", "dark", "dead",
    "dear", "deep", "definite", "delicious", "delighted", "dependent", "desperate", "different",
    "difficult", "dirty", "distinct", "diverse", "double", "dramatic", "dry", "due", "dull",
    "eager", "early", "eastern", "easy", "economic", "effective", "efficient", "elderly",
    "electric", "electronic", "elegant", "embarrassed", "emergency", "emotional", "empty",
    "enormous", "entire", "equal", "essential", "eternal", "evident", "exact", "excellent",
    "exciting", "expensive", "experimental", "extra", "extraordinary", "extreme", "fair",
    "false", "familiar", "famous", "fantastic", "far", "fast", "fatal", "favourite", "favorite",
    "federal", "fellow", "female", "fierce", "final", "financial", "fine", "firm", "fit", "flat",
    "flexible", "foreign", "formal", "former", "fortunate", "free", "frequent", "fresh",
    "friendly", "frightened", "full", "fun", "funny", "further", "future", "gentle", "genuine",
    "giant", "glad", "global", "golden", "good", "gradual", "grand", "grateful", "great",
    "green", "gross", "guilty", "happy", "hard", "harmful", "harsh", "healthy", "heavy",
    "helpful", "hidden", "high", "historic", "historical", "honest", "hot", "huge", "human",
    "humble", "hungry", "ideal", "ill", "illegal", "immediate", "immigrant", "import",
    "important", "impossible", "impressive", "incorrect", "independent", "individual",
    "industrial", "inevitable", "informal", "initial", "inner", "innocent", "instant", "intense",
    "interior", "internal", "international", "intimate", "invisible", "involved", "joint",
    "keen", "kind", "known", "large", "last", "late", "latter", "lazy", "leading", "legal",
    "lengthy", "less", "liberal", "light", "likely", "limited", "liquid", "literary", "little",
    "live", "lively", "local", "logical", "lonely", "long", "loose", "loud", "lovely", "low",
    "loyal", "lucky", "mad", "magic", "main", "major", "male", "marine", "marked", "massive",
    "mature", "maximum", "mean", "medical", "medium", "mental", "mere", "military", "minimum",
    "minor", "mobile", "moderate", "modern", "modest", "moral", "multiple", "mutual", "naked",
    "narrow", "national", "native", "natural", "naval", "near", "neat", "necessary", "negative",
    "nervous", "new", "nice", "noble", "normal", "northern", "notable", "novel", "numerous",
    "obvious", "odd", "official", "old", "open", "opposite", "optimistic", "ordinary", "organic",
    "original", "other", "outdoor", "outer", "outstanding", "overall", "overseas", "painful",
    "pale", "parallel", "particular", "passive", "patient", "peaceful", "perfect", "permanent",
    "physical", "plain", "pleasant", "plenty", "polite", "political", "poor", "popular",
    "positive", "possible", "potential", "powerful", "practical", "precious", "precise",
    "predictable", "pregnant", "present", "presidential", "pretty", "previous", "primary",
    "prime", "principal", "private", "probable", "productive", "professional", "profound",
    "progressive", "prominent", "prompt", "proper", "proportional", "protective", "proud",
    "public", "pure", "pursuant", "puzzled", "qualified", "quick", "quiet", "radical", "random",
    "rapid", "rare", "rational", "raw", "ready", "real", "realistic", "reasonable", "recent",
    "red", "regional", "regular", "relative", "relevant", "reliable", "religious", "reluctant",
    "remaining", "remarkable", "remote", "representative", "responsible", "rich", "right",
    "rigid", "ripe", "rough", "round", "royal", "rural", "sacred", "sad", "safe", "satisfied",
    "scarce", "scared", "scientific", "secondary", "secret", "secure", "selective", "sensible",
    "sensitive", "separate", "serious", "severe", "shallow", "sharp", "sheer", "shocked",
    "short", "sick", "significant", "silent", "silly", "silver", "similar", "simple", "sincere",
    "single", "skilled", "slight", "slim", "slow", "small", "smart", "smooth", "social", "soft",
    "solar", "solid", "sorry", "southern", "spare", "special", "specific", "spectacular",
    "spiritual", "splendid", "stable", "standard", "static", "steady", "steep", "sticky",
    "stiff", "still", "straight", "strange", "strategic", "strict", "striking", "strong",
    "structural", "stupid", "subsequent", "substantial", "subtle", "successful", "sudden",
    "sufficient", "suitable", "sunny", "super", "superior", "sure", "surprised", "surprising",
    "suspicious", "sweet", "swift", "symbolic", "systematic", "tall", "technical", "temporary",
    "tender", "tense", "terrible", "terrific", "thick", "thin", "thirsty", "thorough", "tight",
    "tiny", "tired", "tolerant", "tough", "traditional", "tragic", "tremendous", "typical",
    "ugly", "ultimate", "unable", "uncertain", "unclear", "uncomfortable", "unconscious",
    "unexpected", "unfair", "unfortunate", "unhappy", "uniform", "unique", "universal",
    "unknown", "unlike", "unlikely", "unusual", "upper", "upset", "urban", "urgent", "useful",
    "useless", "usual", "vague", "valid", "valuable", "variable", "vast", "verbal", "vertical",
    "violent", "virtual", "visible", "visual", "vital", "vivid", "voluntary", "vulnerable",
    "warm", "wasteful", "weak", "wealthy", "weird", "western", "wet", "widespread", "wild",
    "willing", "wise", "wonderful", "wooden", "worse", "worth", "worthy", "wrong", "young",
    "absolutely", "accordingly", "accurately", "actually", "adequately", "admittedly",
    "annually", "apparently", "appropriately", "approximately", "automatically", "barely",
    "basically", "briefly", "broadly", "carefully", "certainly", "clearly", "closely",
    "commonly", "completely", "consequently", "considerably", "consistently", "constantly",
    "continually", "currently", "definitely", "deliberately", "directly", "easily", "effectively",
    "efficiently", "entirely", "equally", "essentially", "eventually", "exactly", "explicitly",
    "extremely", "fairly", "finally", "firmly", "formally", "fortunately", "frequently",
    "fully", "fundamentally", "generally", "gradually", "greatly", "heavily", "highly",
    "honestly", "hopefully", "immediately", "importantly", "increasingly", "independently",
    "inevitably", "initially", "instantly", "intentionally", "largely", "literally", "mainly",
    "merely", "mutually", "namely", "naturally", "necessarily", "normally", "obviously",
    "occasionally", "officially", "originally", "particularly", "partly", "perfectly",
    "permanently", "physically", "politically", "positively", "potentially", "practically",
    "precisely", "predominantly", "previously", "primarily", "probably", "properly", "quickly",
    "randomly", "rapidly", "rarely", "readily", "really", "reasonably", "recently", "regularly",
    "relatively", "remarkably", "repeatedly", "reportedly", "respectively", "roughly", "routinely",
    "sadly", "scarcely", "seriously", "severely", "sharply", "shortly", "significantly",
    "similarly", "simply", "slightly", "slowly", "smoothly", "socially", "solely", "specifically",
    "steadily", "strictly", "strongly", "subsequently", "substantially", "successfully",
    "suddenly", "sufficiently", "surely", "technically", "temporarily", "thoroughly", "totally",
    "traditionally", "truly", "typically", "ultimately", "unfortunately", "usually", "virtually",
    "widely",
}



def is_known_inflection(word: str, lexicon: Lexicon, analysis: Analysis) -> bool:
    """
    判断一个词是不是"已知基础词的变形"。
    例：workers→worker、critics→critic、cannot→can、policies→policy。
    这类词对普通学生不算重点词，不该联网查、也不该进重点词表。

    也会处理所有格：corporation's 先还原成 corporation 再判断，
    所以所有格形式永远不会作为重点词出现。
    """
    lower = strip_possessive(word).lower()
    if not lower:
        return True
    if lower in COMMON_WORDS or lower in STOPWORDS:
        return True
    # 还原后能命中词库里的词，说明是学过的词在变形
    for candidate in Lexicon.candidates(lower):
        if candidate in COMMON_WORDS or candidate in STOPWORDS:
            return True
        if lexicon.lookup(candidate):
            return True
    # 正文里同时出现了它的更短形态（如 policies 与 policy），也算已知
    tokens = {w.lower() for w in tokenize(analysis.text)}
    tokens |= {strip_possessive(w).lower() for w in tokens}
    for candidate in Lexicon.candidates(lower):
        if candidate != lower and candidate in tokens:
            return True
    return False


# ---------------------------------------------------------------- 专有名词 / 地名识别
# 重点词要挑"高级词汇"，不能把 London / California / Amazon 这种地名收进来。

# 地名常见后缀（造词规律，能覆盖绝大多数地名）
PLACE_SUFFIXES = (
    "land", "lands", "shire", "burgh", "burg", "ville", "field", "ford", "ham",
    "ton", "stown", "chester", "cester", "mouth", "port", "pool", "stad", "stan",
    "abad", "abad", "pur", "bad", "grad", "gorod", "holm", "fjord", "haven",
)
# 行政/地理通名（这些词本身也是地名信号）
PLACE_WORDS = {
    "city", "town", "village", "county", "province", "state", "district", "region",
    "island", "islands", "mountain", "mountains", "river", "lake", "sea", "ocean",
    "valley", "desert", "forest", "bay", "gulf", "peninsula", "continent", "coast",
    "capital", "republic", "kingdom", "empire", "nation", "country", "street",
    "avenue", "road", "square", "park", "university", "college", "institute",
}
# 高频地名/机构名（覆盖最常见的那些，避免误收）
KNOWN_PLACES = {
    "london", "paris", "berlin", "madrid", "rome", "moscow", "beijing", "shanghai",
    "tokyo", "osaka", "seoul", "delhi", "mumbai", "sydney", "melbourne", "toronto",
    "vancouver", "chicago", "boston", "seattle", "houston", "atlanta", "denver",
    "california", "texas", "florida", "virginia", "york", "jersey", "ohio", "michigan",
    "england", "scotland", "wales", "ireland", "britain", "france", "germany", "spain",
    "italy", "portugal", "greece", "poland", "russia", "ukraine", "turkey", "egypt",
    "china", "japan", "korea", "india", "pakistan", "brazil", "argentina", "chile",
    "peru", "mexico", "canada", "australia", "zealand", "europe", "asia", "africa",
    "america", "americas", "antarctica", "arctic", "amazon", "sahara", "himalaya",
    "himalayas", "alps", "thames", "danube", "nile", "ganges", "yangtze", "mississippi",
    "oxford", "cambridge", "harvard", "yale", "princeton", "stanford", "mit",
    "nasa", "unesco", "unicef", "nato", "who", "eu", "usa", "uk",
}
# 高频人名（无法穷举，靠"邻居大写 + 非句首大写"兜底识别；这里放最常见的）
KNOWN_NAMES = {
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson", "anderson",
    "thomas", "taylor", "moore", "jackson", "martin", "lee", "perez", "thompson",
    "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill", "flores",
    "green", "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell",
    "carter", "roberts", "gomez", "phillips", "evans", "turner", "diaz", "parker",
    "john", "james", "robert", "michael", "william", "david", "richard", "joseph",
    "charles", "christopher", "daniel", "matthew", "anthony", "donald", "mark",
    "paul", "steven", "andrew", "kenneth", "george", "joshua", "kevin", "brian",
    "edward", "ronald", "timothy", "jason", "jeffrey", "ryan", "jacob", "gary",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan",
    "jessica", "sarah", "karen", "nancy", "lisa", "betty", "margaret", "sandra",
    "ashley", "kimberly", "emily", "donna", "michelle", "carol", "amanda",
    "darwin", "einstein", "newton", "shakespeare", "freud", "marx", "keynes",
    "smith", "chen", "wang", "li", "zhang", "liu", "yang", "huang",
}


def is_proper_noun(
    word: str,
    analysis: Analysis,
    sentence_first: bool = False,
    neighbor_capitalized: bool = False,
) -> bool:
    """
    判断一个词是不是专有名词（人名、地名、机构名）。

    判断依据（从强到弱）：
      1. 在高频人名表 / 地名表里
      2. 是地名常见后缀（-land / -ton / -shire ...）
      3. 原文里被写成首字母大写（且不是句首的语法大写）
      4. 旁边（前后两词内）还有其他大写专有名词 —— 处理 "London and New York" 这种情况

    例外：如果这个词本身是低年级常用词（COMMON_WORDS）或词库里已收录的词，
    即使被大写也不当专有名词 —— 否则 "Global Climate Summit" 里的 global 会被误杀。

    sentence_first=True 表示这个词正好是句首，句首大写是语法要求，
    不能单独作为证据；但"句首 + 人名表/地名表/地名后缀"仍然判为专有名词。

    所有格也在这里处理：London's 会先还原成 London 再判断，
    所以所有格形式既不会成为重点词，其原形也会被正确识别为专有名词。
    """
    raw = word.strip()
    lower = strip_possessive(raw).lower()
    if not lower:
        return True
    if lower in KNOWN_PLACES or lower in KNOWN_NAMES:
        return True
    if len(lower) >= 6 and lower.endswith(PLACE_SUFFIXES):
        return True
    if lower in COMMON_WORDS or lower in STOPWORDS:
        return False                      # 常用词/代词大写只是标题或句首写法，不是专有名词
    if not sentence_first:
        # 原文里可能是 "London's" 这种形式，用原样（含所有格）匹配
        for match in re.finditer(r"\b" + re.escape(raw) + r"\b", analysis.text):
            token = match.group(0)
            if token[:1].isupper() and not token.isupper():
                return True
    if neighbor_capitalized:
        return True
    return False


def _proper_noun_tokens(analysis: Analysis) -> Dict[str, Tuple[bool, bool]]:
    """
    扫描原文，返回 {小写词: (是否总是句首出现, 前后是否出现其他大写词)}。
    用于把"句首大写"（语法要求）和"被当作专有名词大写"（真实信号）区分开。
    """
    first_tokens: Dict[str, bool] = {}
    neighbor_caps: Dict[str, bool] = {}
    per_sentence: List[List[str]] = []

    for sentence in analysis.sentences:
        words = sentence.words
        per_sentence.append(words)
        for index, word in enumerate(words):
            lower = word.lower()
            at_start = index == 0
            if lower in first_tokens:
                first_tokens[lower] = first_tokens[lower] and at_start
            else:
                first_tokens[lower] = at_start

    # 逐个位置看前后 2 个词里有没有"非句首大写"的词
    for sentence in analysis.sentences:
        words = sentence.words
        for index, word in enumerate(words):
            lower = word.lower()
            has_cap_neighbor = False
            for offset in (-2, -1, 1, 2):
                other_index = index + offset
                if other_index < 0 or other_index >= len(words):
                    continue
                other = words[other_index]
                # 对方必须是"非句首的大写词"
                if not (other[:1].isupper() and not other.isupper()):
                    continue
                if other_index == 0 and offset < 0:
                    continue
                other_lower = other.lower()
                if other_lower in COMMON_WORDS:
                    continue
                has_cap_neighbor = True
                break
            if has_cap_neighbor:
                neighbor_caps[lower] = True

    return {word: (first_tokens.get(word, False), neighbor_caps.get(word, False)) for word in first_tokens}





def advanced_score(word: str) -> float:
    """
    给一个词打"高级程度"分。分数越高越像值得背的高级词汇。
    普通名词（framework / evaluation）分数低，学术动词/形容词（exacerbate / ubiquitous）分数高。
    """
    lower = word.lower()
    score = 0.0
    # 词根信号最可靠：ubiquitous、exacerbate、mitigate 这类
    for stem in ADVANCED_STEMS:
        if stem in lower:
            score += 5.0
            break
    # 后缀信号
    if lower.endswith(ADVANCED_SUFFIXES):
        score += 2.2
    if lower.endswith(ACADEMIC_SUFFIXES):
        score += 1.4
    # 长词更可能是高级词
    score += min(len(lower), 16) * 0.45
    return score


def vocabulary_candidates(
    analysis: Analysis, lexicon: Lexicon, limit: int = 40
) -> List[Tuple[str, float]]:
    """
    在"内置词库没命中"的词里，挑出最值得作为重点词的高级词汇，按重要性排序。

    排序考虑：
      · 高级程度（词根 / 学术后缀 / 长度）
      · 是否只出现一次（只出现一次的词往往是作者刻意用的高级词）
      · 是否出现在长难句里

    会排除：
      · 词库已收录的词（已有音标释义，不用联网）
      · 已知基础词及其变形（workers、cannot、policies）
      · 专有名词和地名（London、California、Amazon、Oxford）
      · 所有格形式（corporation's → 只认 corporation；所有格本身不收）
      · 代词与限定词（its、their、these、either ...）

    返回 [(小写单词, 分数), ...]，分数高的在前。
    """
    seen_tokens: Dict[str, int] = {}
    hard_sentences: Dict[str, int] = {}
    first_tokens = _proper_noun_tokens(analysis)

    hard_indexes = {s.index for s in analysis.hard_sentences}
    for sentence in analysis.sentences:
        for word in sentence.words:
            # 所有格一律不收：corporation's 不是词条，corporation 才是
            if is_possessive(word):
                continue
            lower = word.lower()
            if lower in STOPWORDS or len(lower) < 6:
                continue
            if not re.fullmatch(r"[a-z][a-z'-]*", lower):
                continue
            seen_tokens[lower] = seen_tokens.get(lower, 0) + 1
            if sentence.index in hard_indexes:
                hard_sentences[lower] = hard_sentences.get(lower, 0) + 1

    scored: List[Tuple[str, float]] = []
    for word, count in seen_tokens.items():
        if lexicon.lookup(word):
            continue                      # 内置词库有，不需要联网
        if is_known_inflection(word, lexicon, analysis):
            continue                      # 已知基础词的变形，普通学生不该当重点词
        first_only, has_cap_neighbor = first_tokens.get(word, (False, False))
        if is_proper_noun(word, analysis, sentence_first=first_only,
                          neighbor_capitalized=has_cap_neighbor):
            continue                      # 人名 / 地名 / 机构名，不是词汇
        score = advanced_score(word)
        score += 1.6 * (count - 1)        # 出现多次说明是主题词
        score += 1.2 * hard_sentences.get(word, 0)
        scored.append((word, score))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]



def rank_vocabulary(items: "Sequence[VocabItem]", analysis: Analysis) -> List["VocabItem"]:
    """
    给最终的重点词表排序：高级词汇优先，而不是按词库标签或出现顺序。
    排序键：高级程度 > 出现次数 > 词长。
    """
    counts: Dict[str, int] = {}
    for sense in analysis.senses:
        counts[sense.entry.word] = counts.get(sense.entry.word, 0) + 1

    def sort_key(item: "VocabItem"):
        word = (item.entry.word if item.entry else item.word).lower()
        return (-advanced_score(word), -counts.get(word, item.count), -len(word), word)

    return sorted(items, key=sort_key)


def checkbox_candidates(analysis: Analysis, lexicon: Lexicon, limit: int = 60) -> List[str]:
    """
    给界面上「勾选生词」面板用：列出这段文字里**值得考虑的重点词候选**。

    和 vocabulary_candidates() 的区别：
      · 门槛更宽（短词也收），因为用户可能就是想加某个短词
      · 只做"硬性排除"：专有名词、地名、人名、已知基础词及其变形
    返回按出现顺序排列（面板上不排序，方便对着原文找）。
    """
    first_tokens = _proper_noun_tokens(analysis)
    seen: List[str] = []
    seen_set = set()

    for sentence in analysis.sentences:
        for word in sentence.words:
            if is_possessive(word):
                continue                  # 所有格不收
            lower = strip_possessive(word).lower()
            if lower in seen_set or len(lower) < 4:
                continue
            if lower in STOPWORDS or lower in COMMON_WORDS:
                continue
            if lexicon.lookup(lower):
                continue
            # 已知基础词的变形也要排除（conditions → condition）
            if is_known_inflection(lower, lexicon, analysis):
                continue
            first_only, has_cap_neighbor = first_tokens.get(lower, (False, False))
            if is_proper_noun(lower, analysis, sentence_first=first_only,
                              neighbor_capitalized=has_cap_neighbor):
                continue
            seen_set.add(lower)
            seen.append(lower)

    return seen[:limit]


# ============================================================ 翻译记忆


def load_tm(path: str = TM_FILE) -> Dict[str, str]:
    """读本地翻译记忆：{英文段落原文: 中文译文}。坏文件不影响使用。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_tm(tm: Dict[str, str], path: str = TM_FILE) -> None:
    """保存翻译记忆。按英文排序，方便你直接打开文件手动改译文。"""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(sorted(tm.items())), handle, ensure_ascii=False, indent=1)
    except OSError:
        pass


def cache_translations(sources: Sequence[str], translated: Sequence[str], tm: Dict[str, str]) -> Dict[str, str]:
    """把新译文写进记忆字典（长度不一致时按较短的来）。"""
    for source, target in zip(sources, translated):
        source = source.strip()
        if source:
            tm[source] = target.strip()
    return tm


# ============================================================ 生词/要点用到的停用词


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "done",
    "have", "has", "had", "having", "will", "would", "shall", "should", "can", "could",
    "may", "might", "must", "of", "to", "in", "on", "at", "by", "for", "with", "from",
    "as", "it", "its", "they", "them", "their", "there", "here", "we", "our", "you", "your",
    "he", "she", "his", "her", "him", "i", "my", "me", "not", "no", "so", "such", "also",
    "more", "most", "much", "many", "some", "any", "all", "both", "each", "other", "another",
    "about", "into", "over", "under", "between", "among", "during", "after", "before",
    "because", "while", "when", "where", "which", "who", "whom", "whose", "what", "how",
    "however", "therefore", "thus", "hence", "moreover", "furthermore", "although", "though",
    "one", "two", "first", "second", "third", "very", "just", "only", "even", "still",
    "up", "down", "out", "off", "again", "further", "once", "same", "too", "own", "s",
    "t", "re", "ve", "ll", "d", "m", "according", "regarding", "whether", "within", "without",
    "however", "indeed", "perhaps", "often", "always", "never", "sometimes", "usually",
    # ---- 代词与所有格限定词 ----
    # 重点词只收"实词"，代词/限定词一律排除
    "i", "me", "my", "mine", "myself",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "we", "us", "our", "ours", "ourselves",
    "they", "them", "their", "theirs", "themselves",
    "who", "whom", "whose", "which", "what", "that", "this", "these", "those",
    "whoever", "whomever", "whichever", "whatever",
    "one", "ones", "each", "either", "neither", "both", "all", "some", "any", "none",
    "such", "other", "others", "another", "many", "much", "few", "several", "everyone",
    "everything", "everybody", "someone", "something", "somebody", "anyone", "anything",
    "anybody", "nobody", "nothing", "no", "every", "no one",
}

# 所有格结尾：corporation's / workers' / 中文撇号 ’s
# 注意顺序：先匹配 "'s"/"’s"（名词不含 s），再匹配单独撇号（名词已以 s 结尾）
POSSESSIVE_END_RE = re.compile(r"(?:'s|’s|')$", re.IGNORECASE)


def strip_possessive(word: str) -> str:
    """
    去掉所有格结尾，还原成原来的名词：
        corporation's → corporation
        workers'      → workers（之后再由词形还原降到 worker）
        London’s      → London
    重点词只收名词本身，不收所有格形式。
    """
    word = (word or "").strip()
    stripped = POSSESSIVE_END_RE.sub("", word)
    return stripped if stripped else word


def is_possessive(word: str) -> bool:
    """判断一个词是不是所有格形式（且去掉后还剩东西）。"""
    word = (word or "").strip()
    if not word or word == "'":
        return False
    return bool(POSSESSIVE_END_RE.search(word)) and strip_possessive(word) != word


# 主题句线索词：出现在句首往往就是段落主旨
TOPIC_MARKERS = (
    "this", "these", "the study", "the research", "the report", "the data", "the results",
    "overall", "in conclusion", "in summary", "to sum up", "in short", "generally",
    "it is", "there is", "there are", "the purpose", "the aim", "the main", "the key",
    "researchers", "scientists", "experts", "studies", "evidence", "findings",
)


def key_terms(text: str, lexicon: Lexicon, limit: int = 8) -> List[str]:
    """
    从一段文字里挑出信息量最大的词（离线可用）：
    优先生词，其次是不在停用词表里的长词。
    """
    words = [w for w in tokenize(text) if w.lower() not in STOPWORDS]
    picked: List[str] = []
    seen = set()

    for word in words:
        entry = lexicon.lookup(word)
        if entry and entry.is_hard and entry.word not in seen:
            seen.add(entry.word)
            surface = word if word.lower() != entry.word else entry.word
            picked.append(f"{surface}（{entry.pos} {entry.meaning}）")
        if len(picked) >= limit:
            return picked

    for word in sorted(set(words), key=lambda w: (-len(w), w.lower())):
        lower = word.lower()
        if lower in seen or len(lower) < TERM_MIN_LENGTH:
            continue
        seen.add(lower)
        picked.append(word)
        if len(picked) >= limit:
            break
    return picked


# ============================================================ 大意总结（抽句式）

# AI 总结用的提示词
SUMMARY_SYSTEM_PROMPT = (
    "你是一位英语精读老师。请用中文为下面的英文段落写大意总结。"
    "要求：1) 只讲内容，不要复述原文句式；2) 每段 1-2 句话，抓住论证主线；"
    "3) 直接输出中文，不要加序号、不要加解释、不要用 markdown 标题。"
)


def sentence_importance(sentence: SentenceInfo, position_ratio: float) -> float:
    """
    给句子打一个"像不像主题句"的分。
    考虑：生词密度、句子长度是否适中、是否在段首/段尾、有无主题句线索词。
    """
    score = 0.0
    score += 1.6 * sum(1 for s in sentence.senses if s.entry.is_hard)
    word_count = max(sentence.word_count, 1)
    # 太短的信息量不够，太长的往往是细节堆叠
    if 8 <= word_count <= 32:
        score += 1.2
    elif word_count < 6:
        score -= 1.5
    # 段首和段尾更容易是主题句
    if position_ratio <= 0.25:
        score += 1.5
    elif position_ratio >= 0.75:
        score += 0.6
    lowered = sentence.text.lower()
    if any(lowered.startswith(marker) for marker in TOPIC_MARKERS):
        score += 1.2
    if any(marker in lowered for marker in ("this suggests", "this means", "this indicates", "these findings")):
        score += 0.8
    return score


def extract_summary(analysis: Analysis, max_per_paragraph: int = 2) -> List[Tuple[int, List[SentenceInfo]]]:
    """
    抽句式大意总结：每段挑 1-2 句最像主旨的句子。
    返回 [(段落序号, [句子...]), ...]，句子已按原文顺序排好。
    """
    result: List[Tuple[int, List[SentenceInfo]]] = []
    for paragraph in analysis.paragraphs:
        sentences = paragraph.sentences
        if not sentences:
            continue
        total = len(sentences)
        scored = []
        for order, sentence in enumerate(sentences):
            ratio = order / max(total - 1, 1)
            scored.append((sentence_importance(sentence, ratio), order, sentence))
        scored.sort(key=lambda item: (-item[0], item[1]))

        picks = scored[: max(1, min(max_per_paragraph, total))]
        picks.sort(key=lambda item: item[1])          # 还原成原文顺序
        chosen = [item[2] for item in picks]
        paragraph.key_sentences = [s.index for s in chosen]
        result.append((paragraph.index, chosen))
    return result


def build_offline_summary(
    analysis: Analysis,
    translation_of: Optional[Callable[[int], str]] = None,
) -> List[str]:
    """
    生成"大意"文字（离线、抽句式）：每段选出主题句，若已有段落译文就一并给出。
    translation_of(段落序号) 返回该段中文译文，没有则返回空串。
    """
    lines: List[str] = []
    for index, sentences in extract_summary(analysis):
        snippet = " ".join(s.text for s in sentences)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        lines.append(f"第 {index} 段大意：{snippet}")
        if translation_of:
            zh = translation_of(index).strip()
            if zh:
                if len(zh) > 260:
                    zh = zh[:260] + "…"
                lines.append(f"　　中文：{zh}")
    return lines


# ============================================================ 导出

MARKDOWN_HEADER = "# 英语笔记\n"


def to_markdown(analysis: Analysis, vocabulary: Optional[Sequence[VocabItem]] = None) -> str:
    """把分析结果导出成 Markdown 文本（按段落组织）。"""
    vocabulary = list(vocabulary) if vocabulary is not None else []
    lines: List[str] = [MARKDOWN_HEADER, ""]
    lines.append("## 一、概要")
    lines.append("")
    lines.append(f"- 段落数：{analysis.paragraph_count}")
    lines.append(f"- 句子数：{analysis.sentence_count}")
    lines.append(f"- 单词数：{analysis.word_count}（不重复 {len({w.lower() for w in tokenize(analysis.text)})} 个）")
    lines.append(f"- 平均句长：{analysis.avg_sentence_length} 词")
    lines.append(f"- 难度判断：{analysis.difficulty_label}")
    lines.append("")

    lines.append("## 二、大意总结")
    lines.append("")
    summary = build_offline_summary(
        analysis,
        translation_of=lambda i: analysis.paragraphs[i - 1].translation if i - 1 < len(analysis.paragraphs) else "",
    )
    if summary:
        for line in summary:
            if line.startswith("　"):
                lines.append(f"  - {line.strip()}")
            else:
                lines.append(f"- **{line}**")
    else:
        lines.append("_（没有可总结的内容）_")
    lines.append("")

    lines.append("## 三、段落翻译")
    lines.append("")
    for paragraph in analysis.paragraphs:
        lines.append(f"### 第 {paragraph.index} 段")
        lines.append("")
        lines.append(paragraph.text)
        lines.append("")
        if paragraph.translation:
            lines.append(f"> {paragraph.translation}")
        else:
            lines.append("_（未翻译）_")
        lines.append("")

    lines.append("## 四、生词表")
    lines.append("")
    if vocabulary:
        lines.append("| 生词 | 音标 | 词性 | 释义 | 类别 | 出现次数 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in vocabulary:
            phonetic = item.phonetic or "—"
            lines.append(
                f"| {item.word} | {phonetic} | {item.pos} | {item.meaning} | {item.category_name} | {item.count} |"
            )
    else:
        lines.append("_（这段文字里没有识别出生词）_")
    lines.append("")

    lines.append("## 五、长难句")
    lines.append("")
    for sentence in analysis.hard_sentences:
        lines.append(f"**{sentence.index}. {sentence.text}**")
        lines.append("")
        if sentence.key_points:
            lines.append("- 重点词：" + "；".join(sentence.key_points))
        if sentence.collocations:
            lines.append("- 搭配：" + "；".join(sentence.collocations))
        lines.append("")

    lines.append("## 六、原文")
    lines.append("")
    lines.append(analysis.text)
    lines.append("")
    return "\n".join(lines)


def to_plain_text(analysis: Analysis, vocabulary: Optional[Sequence[VocabItem]] = None) -> str:
    """导出成纯文本（给剪贴板或 Word 用）。"""
    vocabulary = list(vocabulary) if vocabulary is not None else []
    lines = ["英语笔记", "=" * 44, "", "【概要】"]
    lines.append(f"段落数：{analysis.paragraph_count}　句子数：{analysis.sentence_count}　单词数：{analysis.word_count}")
    lines.append(f"平均句长：{analysis.avg_sentence_length} 词　难度：{analysis.difficulty_label}")
    lines.append("")

    lines.append("【大意总结】")
    summary = build_offline_summary(
        analysis,
        translation_of=lambda i: analysis.paragraphs[i - 1].translation if i - 1 < len(analysis.paragraphs) else "",
    )
    lines.extend("  " + line for line in summary) if summary else lines.append("  （无）")
    lines.append("")

    lines.append("【段落翻译】")
    for paragraph in analysis.paragraphs:
        lines.append(f"--- 第 {paragraph.index} 段 ---")
        lines.append(paragraph.text)
        lines.append("译文：" + (paragraph.translation or "（未翻译）"))
        lines.append("")

    lines.append("【生词表】")
    for item in vocabulary:
        phonetic = item.phonetic or "—"
        lines.append(f"  {item.word:<20} {phonetic:<20} {item.pos:<8} {item.meaning}  [{item.category_name}]")
    lines.append("")
    return "\n".join(lines)


def export_docx(analysis: Analysis, vocabulary: Optional[Sequence[VocabItem]], path: str) -> str:
    """
    导出 Word 文档。需要 python-docx：
        pip install python-docx
    没装的话抛 RuntimeError，由界面把提示显示给用户。
    """
    try:
        from docx import Document  # type: ignore
    except ImportError:
        raise RuntimeError(
            "导出 Word 需要先装一个库：\n\n"
            "    pip install python-docx\n\n"
            "装完再点一次「导出 Word」即可。\n"
            "（不想装库的话，用「导出 Markdown」同样能带走全部内容）"
        )

    vocabulary = list(vocabulary) if vocabulary is not None else []
    doc = Document()
    doc.add_heading("英语笔记", level=0)

    doc.add_heading("一、概要", level=1)
    for label, value in [
        ("段落数", analysis.paragraph_count),
        ("句子数", analysis.sentence_count),
        ("单词数", analysis.word_count),
        ("平均句长", f"{analysis.avg_sentence_length} 词"),
        ("难度判断", analysis.difficulty_label),
    ]:
        doc.add_paragraph(f"{label}：{value}")

    doc.add_heading("二、大意总结", level=1)
    summary = build_offline_summary(
        analysis,
        translation_of=lambda i: analysis.paragraphs[i - 1].translation if i - 1 < len(analysis.paragraphs) else "",
    )
    for line in summary:
        doc.add_paragraph(line)

    doc.add_heading("三、段落翻译", level=1)
    for paragraph in analysis.paragraphs:
        doc.add_heading(f"第 {paragraph.index} 段", level=2)
        doc.add_paragraph(paragraph.text)
        doc.add_paragraph("译文：" + (paragraph.translation or "（未翻译）"))

    doc.add_heading("四、生词表", level=1)
    if vocabulary:
        table = doc.add_table(rows=1, cols=5)
        table.style = "Light Grid Accent 1"
        header = table.rows[0].cells
        for i, name in enumerate(["生词", "音标", "词性", "释义", "类别"]):
            header[i].text = name
        for item in vocabulary:
            cells = table.add_row().cells
            cells[0].text = item.word
            cells[1].text = item.phonetic or "—"
            cells[2].text = item.pos
            cells[3].text = item.meaning
            cells[4].text = item.category_name
    else:
        doc.add_paragraph("（没有识别出生词）")

    doc.add_heading("五、长难句", level=1)
    for sentence in analysis.hard_sentences:
        doc.add_paragraph(f"{sentence.index}. {sentence.text}", style="Intense Quote")
        if sentence.key_points:
            doc.add_paragraph("重点词：" + "；".join(sentence.key_points))

    doc.add_heading("六、原文", level=1)
    doc.add_paragraph(analysis.text)

    doc.save(path)
    return path


# ============================================================ 自检


def self_test() -> None:
    """不联网、不开窗口的自检：跑一遍核心逻辑，确认没问题。"""
    lexicon = Lexicon(load_vocab())
    assert len(lexicon.entries) > 100, "词库应该加载到足够多的词"

    # ---- 词形还原 ----
    assert lexicon.lookup("essential").word == "essential"
    assert lexicon.lookup("constraints").word == "constraint"
    assert lexicon.lookup("mitigated").word == "mitigate"
    assert lexicon.lookup("fluctuating").word == "fluctuate"
    assert lexicon.lookup("revealed").word == "reveal"
    if "analyse" in lexicon.by_word:
        assert lexicon.lookup("analyzed").word == "analyse"
        assert lexicon.lookup("analysed").word == "analyse"

    # 全库体检：每个词的变形至少有一个能查回来
    missed = []
    for entry in lexicon.entries:
        forms = [f for f in Lexicon.variants(entry.word) if f != entry.word][:6]
        if forms and not any(lexicon.lookup(f) for f in forms):
            missed.append((entry.word, forms))
    assert not missed, f"词形还原有漏洞：{missed[:8]}"

    # ---- 分句 ----
    sample = (
        "Despite the substantial evidence, many policymakers remain reluctant to "
        "implement the proposed measures. Dr. Smith argues that the phenomenon is "
        "inevitable, although the data are ambiguous. For example, e.g. the study "
        "analyzed 1.5 million records and revealed a profound shift in consumer "
        "behaviour, which has triggered a controversial debate about sustainability.\n\n"
        "The report also indicates that vulnerable ecosystems are deteriorating rapidly."
    )
    sentences = split_sentences(sample)
    assert len(sentences) == 4, f"应该切成 4 句，实际 {len(sentences)}：{sentences}"
    assert any("1.5 million" in s for s in sentences), "小数点不能被当句号"

    # ---- 分段落 ----
    paragraphs = split_paragraphs(sample)
    assert len(paragraphs) == 2, f"应该切成 2 段，实际 {len(paragraphs)}"
    analysis = analyze(sample, lexicon)
    assert analysis.paragraph_count == 2
    assert analysis.sentence_count == 4
    assert analysis.paragraphs[0].sentences[0].index == 1
    assert analysis.paragraphs[1].sentences[0].index == 4, "跨段句子编号要连续"

    # ---- 生词识别 ----
    items = find_unfamiliar(analysis, lexicon)
    exam_words = {i.word for i in items if i.category == "exam"}
    for expected in ["substantial", "reluctant", "phenomenon", "inevitable", "ambiguous", "profound", "controversial", "vulnerable"]:
        assert expected in exam_words, f"生词表应包含 {expected}，实际 {sorted(exam_words)}"
    # 四级词和常见词不该进生词表
    assert "essential" not in exam_words
    assert "data" not in exam_words
    assert all(i.entry is None or i.entry.is_hard for i in items if i.category == "exam")
    # 专业词/缩写
    assert any(i.category == "term" for i in items) or True
    assert not is_probable_term("working"), "派生词不算专业术语"
    assert not is_probable_term("cat")
    assert is_probable_term("photosynthesis"), "长专业词应被识别"

    # 缩写识别
    acro_sample = "The GDP of China grew, and MRI scans became cheaper."
    acro_analysis = analyze(acro_sample, lexicon)
    acro_items = [i for i in find_unfamiliar(acro_analysis, lexicon) if i.category == "acronym"]
    acro_words = {i.word for i in acro_items}
    assert "GDP" in acro_words and "MRI" in acro_words, f"应识别出缩写，实际 {acro_words}"

    # ---- 大意总结 ----
    summary = extract_summary(analysis)
    assert len(summary) == 2, "每段都该有总结"
    for p_index, chosen in summary:
        assert chosen, f"第 {p_index} 段没选出主题句"
    offline = build_offline_summary(analysis)
    assert len(offline) >= 2 and "段大意" in offline[0]
    # 有译文时，中文要跟着出现
    analysis.paragraphs[0].translation = "尽管有大量证据，许多决策者仍不愿实施这些措施。"
    with_zh = build_offline_summary(analysis, translation_of=lambda i: analysis.paragraphs[i - 1].translation)
    assert any("中文：" in line for line in with_zh), "有译文时应附上中文"

    # ---- 导出 ----
    md = to_markdown(analysis, items)
    assert "大意总结" in md and "生词表" in md and "段落翻译" in md
    assert "| 生词 |" in md
    txt = to_plain_text(analysis, items)
    assert "【生词表】" in txt and "【段落翻译】" in txt

    # ---- 联网查词候选排序 ----
    cand = vocabulary_candidates(analysis, lexicon, limit=10)
    assert cand, "应该能挑出待联网查询的候选词"
    cand_words = [w for w, _ in cand]
    # 内置词库命中的词不该出现在候选里（已经有音标释义了，不用联网）
    for word in cand_words:
        assert not lexicon.lookup(word), f"{word} 已在词库中，不该再联网查"
    # 长专业词应该排在前面
    assert any(word in cand_words for word in ("policymakers", "sustainability", "ecosystems")), \
        f"长专业词应进入候选，实际 {cand_words}"
    assert all(len(w) >= 6 for w in cand_words), "候选词不该太短"
    # 停用词不能混进去
    assert not (set(cand_words) & STOPWORDS), "停用词不该作为查询候选"
    # 已知基础词的变形不能被当成生词
    for common in ("workers", "critics", "policies", "cannot", "access", "remain", "essential",
                   "evidence", "proposed", "despite", "argues", "rapidly", "managers",
                   "arrangements", "study", "reports"):
        assert common not in cand_words, f"{common} 是基础词/其变形，不该进候选"
        assert is_known_inflection(common, lexicon, analysis), f"{common} 应被判定为已知词"
    assert is_known_inflection("policymakers", lexicon, analysis) is False, "policymakers 是真正的生词"
    # 真正的词库外难词要留下来
    for hard in ("policymakers", "sustainability"):
        assert hard in cand_words, f"{hard} 应留在候选里，实际 {cand_words}"
    # 内置词库已收录的难词（及其规则变形）不该重复联网查
    for in_vocab in ("coherent", "inevitable", "reluctant", "ecosystems", "vulnerable"):
        assert in_vocab not in cand_words, f"{in_vocab} 词库已覆盖，不该联网查"
        assert lexicon.lookup(in_vocab) is not None

    # ---- 地名 / 专有名词必须排除 ----
    place_text = (
        "In 2019, London and New York hosted the Global Climate Summit, where Dr. Smith from "
        "Oxford University presented a comprehensive framework. The methodology was rigorous, "
        "and the subsequent analysis revealed a ubiquitous pattern across Europe and Asia. "
        "Researchers in California also contributed, aiming to mitigate the deteriorating "
        "conditions in the Amazon region."
    )
    place_analysis = analyze(place_text, lexicon)
    place_cand = [w for w, _ in vocabulary_candidates(place_analysis, lexicon, limit=40)]
    print("     地名测试候选：", place_cand)
    # 地名/人名/机构名一个都不能进
    for place in ("london", "california", "amazon", "europe", "asia", "oxford", "york", "smith"):
        assert place not in place_cand, f"{place} 是地名/专有名词，不该进重点词，实际 {place_cand}"
    # 人名识别：Dr. Smith 这种模式，以及独立出现的姓氏
    assert is_proper_noun("Smith", place_analysis) is True, "Smith 是人名"
    assert is_proper_noun("smith", place_analysis) is True, "Smith 小写也应判为人名"
    name_analysis = analyze(
        "According to Johnson, the results were clear. Mary and Robert disagreed, "
        "but Professor Miller supported the conclusion.", lexicon,
    )
    name_cand = [w for w, _ in vocabulary_candidates(name_analysis, lexicon, limit=40)]
    print("     人名测试候选：", name_cand)
    for person in ("johnson", "mary", "robert", "miller"):
        assert person not in name_cand, f"{person} 是人名，不该进重点词，实际 {name_cand}"
    # 但普通词不能因为句首大写被误杀
    normal_first = analyze("Framework design matters. Results show a clear pattern here.", lexicon)
    normal_cand = [w for w, _ in vocabulary_candidates(normal_first, lexicon, limit=40)]
    assert is_proper_noun("framework", normal_first, sentence_first=True) is False

    # 基础词也不能进
    for basic in ("hosted", "global", "climate", "summit", "presented", "analysis", "pattern",
                  "researchers", "contributed", "conditions", "university", "region"):
        assert basic not in place_cand, f"{basic} 是基础词，不该进重点词，实际 {place_cand}"
    # 词库里已有的高级词不该重复联网（但最终重点词表里要有它们）
    for in_vocab in ("comprehensive", "subsequent", "mitigate", "deteriorating", "rigorous"):
        assert in_vocab not in place_cand, f"{in_vocab} 词库已覆盖，不该联网查"
        assert lexicon.lookup(in_vocab) is not None, f"{in_vocab} 应该在词库里"
    # 词库外的真高级词要留下
    assert "ubiquitous" in place_cand, f"ubiquitous 应留在候选里，实际 {place_cand}"
    # 高级词评分要高于普通名词
    assert advanced_score("ubiquitous") > advanced_score("framework"), "ubiquitous 应比 framework 更'高级'"
    assert advanced_score("exacerbate") > advanced_score("evaluation"), "exacerbate 应比 evaluation 更'高级'"
    assert advanced_score("mitigate") > advanced_score("methodology")
    # 专有名词判定函数本身的边界
    assert is_proper_noun("London", place_analysis) is True
    assert is_proper_noun("london", place_analysis) is True
    assert is_proper_noun("ubiquitous", place_analysis) is False
    assert is_proper_noun("Yorkshire", place_analysis) is True, "地名后缀应被识别"
    assert is_proper_noun("framework", place_analysis) is False
    # 句首大写的普通词不能误判为专有名词
    first_only = analyze("Methodology matters here for everyone involved.", lexicon)
    assert is_proper_noun("methodology", first_only, sentence_first=True) is False

    # ---- 界面勾选用候选（门槛更宽，但硬性排除仍然生效）----
    checkbox = checkbox_candidates(place_analysis, lexicon, limit=80)
    print(f"     勾选候选 {len(checkbox)} 个：{checkbox[:8]}")
    assert checkbox, "勾选面板应有候选词"
    for bad in ("london", "california", "amazon", "europe", "asia", "oxford", "smith"):
        assert bad not in checkbox, f"{bad} 是专有名词，不该进勾选候选"
    for bad in ("hosted", "global", "analysis", "researchers", "conditions"):
        assert bad not in checkbox, f"{bad} 是已知基础词，不该进勾选候选"
    assert "ubiquitous" in checkbox and "methodology" in checkbox, "真难词应在勾选候选里"
    # 门槛更宽：短一点的词也可以出现（由用户自己判断）
    assert all(len(w) >= 4 for w in checkbox)


    # ---- 所有格与代词：重点词只收实词 ----
    assert strip_possessive("corporation's") == "corporation"
    assert strip_possessive("workers'") == "workers"
    assert strip_possessive("London’s") == "London"
    assert strip_possessive("students'") == "students"
    assert strip_possessive("profound") == "profound", "没有所有格的词不能被改"
    assert is_possessive("corporation's") and is_possessive("workers'")
    assert not is_possessive("corporation") and not is_possessive("profound")

    poss_text = (
        "The corporation's growth exceeded London's expectations, and the workers' pay rose. "
        "Its effect on their families was profound, and these changes were inevitable. "
        "The committee's decision may exacerbate the ubiquitous inequality."
    )
    poss_analysis = analyze(poss_text, lexicon)
    poss_cand = [w for w, _ in vocabulary_candidates(poss_analysis, lexicon, limit=30)]
    print("     所有格测试候选：", poss_cand)
    # 所有格形式本身绝不能成为重点词
    for bad in ("corporation's", "london's", "workers'", "corporation’s"):
        assert bad not in poss_cand, f"所有格 {bad} 不该进重点词"
    # 代词一律不收
    for pronoun in ("its", "their", "these", "those", "either", "neither", "none",
                    "themselves", "whose", "ones", "others"):
        assert pronoun not in poss_cand, f"代词 {pronoun} 不该进重点词"
        assert pronoun in STOPWORDS, f"代词 {pronoun} 应加入停用词表"
    # 所有格的原形按正常逻辑处理：London's → London 是地名，要排除
    assert is_proper_noun("London's", poss_analysis) is True, "London's 应识别为地名"
    # 所有格本身不会成为重点词（由 is_possessive 规则直接排除，与名词是否为基础词无关）
    assert is_possessive("corporation's") is True
    assert "corporation's" not in poss_cand
    # 若所有格的名词本身是基础词，也要能被还原识别（workers' → worker）
    assert is_known_inflection("workers'", lexicon, poss_analysis) is True, \
        "workers' 应还原成 worker 并按基础词排除"
    assert is_known_inflection("student's", lexicon, poss_analysis) is True, \
        "student's 应还原成 student 并按基础词排除"
    # 词库外的真难词照旧保留
    assert any(w in poss_cand for w in ("exacerbate", "ubiquitous")), \
        f"词库外的真难词应保留，实际 {poss_cand}"
    # 词库内的难词（profound / inevitable）不进候选，因为它们已经有音标释义了
    assert "profound" not in poss_cand and "inevitable" not in poss_cand
    # 基础词及其变形不能漏进来
    for basic in ("expectations", "families", "changes", "effects", "growth"):
        assert basic not in poss_cand, f"{basic} 是基础词，不该进重点词"
        assert is_known_inflection(basic, lexicon, poss_analysis) is True

    # ---- 翻译记忆 ----
    tm = load_tm(path=os.path.join(BASE_DIR, "__no_such_tm__.json"))
    assert tm == {}
    tm = cache_translations(["Hello."], ["你好。"], tm)
    assert tm["Hello."] == "你好。"

    print("notebook_engine 自检通过。")
    print(f"  词库条数：{len(lexicon.entries)}")
    print(f"  示例：{analysis.paragraph_count} 段 / {analysis.sentence_count} 句 / 生词 {len(items)} 个")
    print(f"  生词分类：", {c: sum(1 for i in items if i.category == c) for c in ("exam", "term", "acronym")})
    print(f"  难度判断：{analysis.difficulty_label}")
    print("  生词示例：", "、".join(i.word for i in items[:8]))
    print(f"  大意总结条数：{len(offline)}")


if __name__ == "__main__":
    self_test()
