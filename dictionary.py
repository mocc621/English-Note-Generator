# -*- coding: utf-8 -*-
"""
dictionary.py —— 运行时联网查词（音标 + 中文释义）

为什么不内置大词库？
    内置词库再大也只有几万词，遇到专业文献还是会漏。改成运行时查：
    先查内置词库（离线、必然有音标和释义），没命中再联网查，并缓存到本地。

数据来源（都是免密钥的公开接口）：
    有道词典  https://dict.youdao.com/jsonapi
        返回 usphone / ukphone（音标）、trs（中文释义，带词性），
        对中文学习者最友好，实测稳定。
    MyMemory  https://api.mymemory.translated.net/get
        兜底：把英文单词整词翻译成中文。质量中等，但胜在免注册。

风险（写在代码里，避免以后忘掉）：
    这两个都是公开网页接口，不是签约的开放平台 API。可能限流、改版或失效。
    所以：① 尽量少请求（本地缓存 + 每轮限流）；② 失败不影响主流程，只是缺释义。

只依赖标准库。
"""

import html
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "dict_cache.json")

YOUDAO_API = "https://dict.youdao.com/jsonapi"
MYMEMORY_API = "https://api.mymemory.translated.net/get"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 联网查词的默认上限：词数、并发、每次请求间隔（秒）
DEFAULT_MAX_WORDS = 30
DEFAULT_WORKERS = 4
DEFAULT_DELAY = 0.25
DEFAULT_TIMEOUT = 12


@dataclass
class DictLookup:
    """一个词的查询结果。"""

    word: str
    phonetic: str = ""      # 形如 /rɪˈlʌktənt/
    pos: str = ""           # 形如 adj.
    meaning: str = ""       # 中文释义
    source: str = ""        # youdao / mymemory / cache
    ok: bool = False

    def as_cache(self) -> dict:
        return {
            "phonetic": self.phonetic,
            "pos": self.pos,
            "meaning": self.meaning,
            "source": self.source,
        }


# ============================================================ 缓存


def load_cache(path: str = CACHE_FILE) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: Dict[str, dict], path: str = CACHE_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=0, sort_keys=True)
    except OSError:
        pass


# ============================================================ HTTP


def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """GET 一个 JSON。失败返回 None —— 查词失败不该让整个程序崩掉。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout,
            json.JSONDecodeError, OSError):
        return None


# ============================================================ 有道


def _clean_text(text: str) -> str:
    """去掉 HTML 标签、统一全角标点、压掉多余空白。"""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = html.unescape(text)
    # 有道会混用全角标点，统一成半角，界面和导出更整齐
    for full, half in (("．", "."), ("：", ":"), ("，", ","), ("；", ";"), ("（", "("), ("）", ")")):
        text = text.replace(full, half)
    return re.sub(r"\s+", " ", text).strip()


def _tidy_meaning(meaning: str) -> Tuple[str, str]:
    """
    整理中文释义，返回 (词性, 清理后的释义)：
      · 把开头的词性标记抽出来（adj. / n. / vt. ...）
      · 清掉每条释义前重复的词性标记
      · 太长就按标点截断，并去掉尾部残留的标点
    多词条释义（"adj. 勉强的; n. 反感"）也能逐段正确处理。
    """
    if not meaning:
        return "", ""

    pos = ""
    match = re.match(r"^\s*((?:[a-z]{1,5}\.\s*)+)", meaning)
    if match:
        pos = match.group(1).strip()

    segments = []
    for seg in meaning.split(";"):
        cleaned = re.sub(r"^\s*(?:[a-z]{1,5}\.\s*)+", "", seg).strip()
        if cleaned:
            segments.append(cleaned)
    text = ";".join(segments) if segments else meaning.strip()

    if len(text) > 60:
        cut = text[:60]
        for sep in (";", ",", "、", ":", "("):
            if sep in cut:
                cut = cut[: cut.rfind(sep)]
                break
        cut = cut.strip().rstrip(";,:、。.…( ")
        text = (cut or text[:60].strip()) + "…"

    return pos, text


def _parse_youdao(data: dict, word: str) -> DictLookup:
    """
    从有道返回里提取音标和中文释义。
    结构（实测）：
        data["ec"]["word"][0]["usphone"] / ["ukphone"]
        data["ec"]["word"][0]["trs"][i]["tr"][j]["l"]["i"][0] = "adj. 勉强的，不情愿的"
    """
    result = DictLookup(word=word, source="youdao")

    ec = data.get("ec") or {}
    words = ec.get("word")
    node = None
    if isinstance(words, list) and words:
        node = words[0]
    elif isinstance(words, dict):
        node = words

    # 音标：优先美音，其次英音，再退回 simple / collins
    if isinstance(node, dict):
        phonetic = node.get("usphone") or node.get("ukphone")
        if not phonetic:
            simple = data.get("simple") or {}
            simple_words = simple.get("word") if isinstance(simple, dict) else None
            if isinstance(simple_words, list) and simple_words:
                phonetic = simple_words[0].get("usphone") or simple_words[0].get("ukphone")
        if phonetic:
            result.phonetic = f"/{str(phonetic).strip().strip('/')}/"

        # 中文释义
        meanings: List[str] = []
        for tr_group in node.get("trs") or []:
            for item in tr_group.get("tr") or []:
                langs = (item.get("l") or {}).get("i")
                if isinstance(langs, list):
                    for one in langs:
                        text = _clean_text(one)
                        if text and text not in meanings:
                            meanings.append(text)
                elif isinstance(langs, str):
                    text = _clean_text(langs)
                    if text:
                        meanings.append(text)
        if meanings:
            result.meaning = "; ".join(meanings[:4])

    # 兜底音标：柯林斯 / magic_words
    if not result.phonetic:
        collins = data.get("collins") or {}
        entries = collins.get("collins_entries") if isinstance(collins, dict) else None
        if isinstance(entries, list) and entries:
            phonetic = entries[0].get("phonetic")
            if phonetic:
                result.phonetic = f"/{str(phonetic).strip().strip('/')}/"
    if not result.phonetic:
        magic = data.get("magic_words") or {}
        items = magic.get("magic_words") if isinstance(magic, dict) else None
        if isinstance(items, list) and items:
            info = items[0].get("info") or []
            if info and info[0].get("phonetic"):
                result.phonetic = str(info[0]["phonetic"]).strip()

    # 兜底释义：simple.explain 或 web_trans
    if not result.meaning:
        simple = data.get("simple") or {}
        simple_words = simple.get("word") if isinstance(simple, dict) else None
        if isinstance(simple_words, list) and simple_words:
            explain = simple_words[0].get("explain")
            if explain:
                result.meaning = _clean_text(explain)[:80]
    if not result.meaning:
        web = data.get("web_trans") or {}
        items = web.get("web-translation") if isinstance(web, dict) else None
        if isinstance(items, list) and items:
            value = items[0].get("value")
            if value:
                result.meaning = _clean_text(value)[:80]

    # 词性 + 释义统一整理（含截断和尾部标点清理）
    result.pos, result.meaning = _tidy_meaning(result.meaning)
    if not result.pos and result.meaning:
        # 词性可能在后来补进来的释义里，再试一次
        result.pos, result.meaning = _tidy_meaning(result.meaning)

    result.ok = bool(result.phonetic or result.meaning)
    return result


def lookup_youdao(word: str, timeout: int = DEFAULT_TIMEOUT) -> DictLookup:
    query = urllib.parse.quote(word)
    url = f"{YOUDAO_API}?q={query}&dict=dict&le=en"
    data = _get_json(url, timeout=timeout)
    if not data:
        return DictLookup(word=word, source="youdao")
    return _parse_youdao(data, word)


# ============================================================ MyMemory 兜底


def lookup_mymemory_batch(words: List[str], timeout: int = DEFAULT_TIMEOUT) -> Dict[str, str]:
    """
    一次请求翻译多个词，返回 {英文词: 中文}。
    MyMemory 用 | 分隔多个词，返回也用 | 分隔，实测可行，能省大量请求。
    """
    if not words:
        return {}
    joined = "|".join(words[:20])
    params = urllib.parse.urlencode({"q": joined, "langpair": "en|zh-CN"})
    data = _get_json(f"{MYMEMORY_API}?{params}", timeout=timeout)
    if not data:
        return {}
    text = (data.get("responseData") or {}).get("translatedText") or ""
    parts = [p.strip() for p in str(text).split("|")]
    out: Dict[str, str] = {}
    if len(parts) == len(words[:20]):
        for word, translation in zip(words[:20], parts):
            translation = _clean_text(translation)
            # 翻不出来时接口常原样返回英文
            if translation and translation.lower() != word.lower():
                out[word] = translation
    return out


# ============================================================ 对外主函数


def enrich_words(
    words: List[str],
    cache: Optional[Dict[str, dict]] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    workers: int = DEFAULT_WORKERS,
    delay: float = DEFAULT_DELAY,
    progress: Optional[Callable[[int, int], None]] = None,
    use_mymemory_fallback: bool = True,
) -> Dict[str, DictLookup]:
    """
    批量补全单词的音标和中文释义。

    words       待查的词（已去重、已按重要性排好序）
    cache       本地缓存字典（会被就地更新，调用方负责保存）
    max_words   本轮最多查多少个，避免把接口打爆
    progress    进度回调 progress(已完成, 总数)

    返回 {词: DictLookup}。查不到的词不会出现在结果里。
    """
    cache = cache if cache is not None else {}
    results: Dict[str, DictLookup] = {}
    todo: List[str] = []

    for word in words:
        key = word.lower()
        cached = cache.get(key)
        if cached and (cached.get("meaning") or cached.get("phonetic")):
            results[key] = DictLookup(
                word=key,
                phonetic=cached.get("phonetic", ""),
                pos=cached.get("pos", ""),
                meaning=cached.get("meaning", ""),
                source="cache",
                ok=True,
            )
        elif key not in todo:
            todo.append(key)

    todo = todo[:max_words]
    total = len(todo)
    if not total:
        if progress:
            progress(0, 0)
        return results

    # 逐个查有道（并发，但要限速）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for index, word in enumerate(todo):
            # 错开提交时间，避免瞬间并发被限流
            time.sleep(delay)
            futures[pool.submit(lookup_youdao, word)] = word
        for future in as_completed(futures):
            word = futures[future]
            try:
                found = future.result()
            except Exception:
                found = DictLookup(word=word, source="youdao")
            done += 1
            if found.ok:
                results[word] = found
                cache[word] = found.as_cache()
            if progress:
                progress(done, total)

    # 有道没查到的，用 MyMemory 批量兜底
    if use_mymemory_fallback:
        missing = [w for w in todo if w not in results]
        if missing:
            fallback = lookup_mymemory_batch(missing)
            for word, meaning in fallback.items():
                item = DictLookup(word=word, meaning=meaning, source="mymemory", ok=True)
                results[word] = item
                cache[word] = item.as_cache()

    return results


# ============================================================ 自检


def self_test(live: bool = False) -> None:
    """
    默认只做离线自检（解析真实返回样本）。
    传 live=True 才真的联网，避免测试时反复打扰接口。
    """
    # 用真实的返回结构做解析测试（字段是从实测响应里摘出来的）
    sample = {
        "ec": {
            "word": [
                {
                    "usphone": "rɪˈlʌktənt",
                    "ukphone": "rɪˈlʌktənt",
                    "trs": [
                        {"tr": [{"l": {"i": ["adj. 勉强的，不情愿的"]}}]},
                        {"tr": [{"l": {"i": ["adj. 顽抗的"]}}]},
                    ],
                }
            ]
        }
    }
    parsed = _parse_youdao(sample, "reluctant")
    assert parsed.phonetic == "/rɪˈlʌktənt/", f"音标解析错误：{parsed.phonetic}"
    assert parsed.pos == "adj.", f"词性解析错误：{parsed.pos}"
    assert "adj." not in parsed.meaning, f"释义不该残留词性标记：{parsed.meaning}"
    assert "勉强的" in parsed.meaning and "顽抗的" in parsed.meaning, f"多条释义应合并：{parsed.meaning}"
    assert parsed.ok
    # 多词条时每一段的词性都要清掉
    multi = _parse_youdao(
        {"ec": {"word": [{"usphone": "x", "trs": [
            {"tr": [{"l": {"i": ["adj. 勉强的"]}}]},
            {"tr": [{"l": {"i": ["n. 反感"]}}]},
            {"tr": [{"l": {"i": ["v. 抵抗"]}}]},
        ]}]}},
        "reluctant",
    )
    assert multi.meaning == "勉强的;反感;抵抗", f"逐段清理失败：{multi.meaning}"
    # 长释义要截断并去掉尾部标点（包括 web_trans 兜底分支）
    long_one = _parse_youdao(
        {"web_trans": {"web-translation": [
            {"value": "特权,特别权利:一个人或团体独有的权利或特权,通常是由法律、习惯或地位赋予的"}
        ]}},
        "prerogative",
    )
    assert len(long_one.meaning) <= 62, f"长释义未截断：{long_one.meaning}"
    assert not long_one.meaning.rstrip().endswith((":", ",", ";", "、")), f"尾部标点没清掉：{long_one.meaning}"
    # 词性要从多词条里正确抽出
    multi_pos = _parse_youdao(
        {"ec": {"word": [{"usphone": "x", "trs": [{"tr": [{"l": {"i": ["adj. 勉强的"]}}]}]}]}},
        "reluctant",
    )
    assert multi_pos.pos == "adj." and multi_pos.meaning == "勉强的", (multi_pos.pos, multi_pos.meaning)

    # 只有 web_trans 的退化响应也不能崩
    degraded = _parse_youdao({"web_trans": {"web-translation": [{"value": "不情愿的"}]}}, "reluctant")
    assert degraded.meaning == "不情愿的" and degraded.ok

    # 空响应不能崩
    empty = _parse_youdao({}, "unknown")
    assert not empty.ok and empty.meaning == ""

    # HTML 清理
    assert _clean_text("<b>不情愿的</b>&amp;勉强的") == "不情愿的&勉强的"

    # 缓存读写
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            assert load_cache(path) == {}
            save_cache({"reluctant": {"phonetic": "/x/", "meaning": "勉强的"}}, path)
            again = load_cache(path)
            assert again["reluctant"]["meaning"] == "勉强的"
    except OSError:
        # 受限环境（例如临时目录只读）写不了文件；这不是代码问题，跳过这一段
        print("  跳过缓存读写检查（临时目录不可写）")

    # 缓存命中时不应发起网络请求
    cache = {"reluctant": {"phonetic": "/rɪˈlʌktənt/", "pos": "adj.", "meaning": "勉强的", "source": "youdao"}}
    got = enrich_words(["reluctant"], cache=cache)
    assert got["reluctant"].source == "cache" and got["reluctant"].meaning == "勉强的"

    print("dictionary 自检通过（解析 / 退化响应 / HTML 清理 / 缓存命中）。")

    if live:
        print("联网实测：")
        for word in ("reluctant", "photosynthesis", "ubiquitous"):
            item = lookup_youdao(word)
            flag = "OK " if item.ok else "MISS"
            print(f"  [{flag}] {word:16} {item.phonetic:20} {item.pos:8} {item.meaning[:46]}")
        batch = lookup_mymemory_batch(["coherent", "inevitable"])
        print("  MyMemory 批量兜底:", batch)


if __name__ == "__main__":
    import sys

    self_test(live="--live" in sys.argv)
