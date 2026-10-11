# -*- coding: utf-8 -*-
"""
tests/test_bookmark_gui.py —— 收藏（☆/★）界面测试（不联网、绝不碰真正的生词本）

用法（在项目根目录）：
    python tests\\test_bookmark_gui.py

它做的事情就是 README 里承诺的那件：
  1. 先把「生词本」的存储位置整个搬到系统临时目录 —— 真正的 生词本/ 一个字节都不会动；
  2. 真的开一个 tkinter 窗口，用本地词库跑一次 engine.analyze（纯本地匹配，不联网）；
  3. 在笔记里那颗 ☆ 上真的生成一次鼠标左键点击，走的是程序自己的真代码路径：
     _on_note_click → _bookmark_toggle → _bookmark / _unbookmark → vocab_book.Store；
  4. 检查临时目录里确实写出了 生词本.json / 生词本.md，再点一次 ★ 取消收藏；
  5. 收尾删掉临时目录，并对比测试前后的真实 生词本/ 快照，确认没被动过。

几个"为什么非得这样写"：
  · Store.load() / Store.__init__ 的 folder 默认值是在**函数定义时**就绑定到
    vocab_book.FOLDER 的（Python 默认参数只求值一次），光改模块常量根本没用 ——
    所以这里连那几个函数的 __defaults__ 一起换成临时目录，改完还要断言真的生效了。
  · 不联网：只调用 engine.analyze（本地词库匹配），不碰翻译 / 查词接口；
    另外全程把 Python 层的 socket 连接掐掉，真去联网会直接报错，而不是"悄悄连上"。
  · 界面测试需要有显示环境：真起不来（没有图形界面）就打印 SKIP 并以 0 退出。
  · 退出码：0 = 全部通过，1 = 有检查失败（失败项会一条条列出来，不吞）。

只依赖标准库（tkinter 随 Python 分发）。
"""

import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import traceback

# 和 diagnose_translation.py 一个套路：脚本在 tests\ 里，模块在上一层（项目根目录）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 控制台编码不一定是 UTF-8，强制一下，保证 ☆ / ★ / ✓ / ✗ 打得出来
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import vocab_book  # noqa: E402  （必须在 sys.path 调整之后再导入）

# 真实的生词本文件夹 —— 测试里只准"看"，不准"写"
REAL_FOLDER = vocab_book.FOLDER

# 顺带看一眼：同一份程序如果还放在别的盘（例如 D:\英语笔记生成器），那边也必须原封不动
OTHER_PROJECT_DIRS = [
    r"D:\英语笔记生成器",
    os.path.join(os.path.dirname(os.path.dirname(ROOT)), os.path.basename(ROOT)),
]

# 测试用的英文：resilient / paradigm 都在内置词库 exam_vocab.txt 里（雅思、考研 → 算高难词），
# 所以不用联网查词也能渲染出重点词那一行（· 单词　▸ 展开　☆）。
SAMPLE_TEXT = (
    "The resilient team recovered quickly from the failure. "
    "A new paradigm emerged from their careful work."
)


# ============================================================ 小工具


class Checker:
    """把每条检查的结果收集起来，最后统一汇总 —— 失败不会被吞掉，退出码非 0。"""

    def __init__(self) -> None:
        self.total = 0
        self.failed = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.total += 1
        if ok:
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name}　{detail}")
            self.failed.append(name)
        return bool(ok)


def snapshot(folder: str):
    """
    给一个文件夹拍快照：{相对路径: (字节数, 内容 sha1)}；文件夹不存在就返回 None。
    用"名字 + 大小 + 内容哈希"比对，比只看修改时间靠谱（复制/回写都可能骗过 mtime）。
    """
    if not os.path.isdir(folder):
        return None
    snap = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                with open(full, "rb") as handle:
                    digest = hashlib.sha1(handle.read()).hexdigest()
                size = os.path.getsize(full)
            except OSError as exc:
                digest, size = f"<读不到：{exc}>", -1
            snap[os.path.relpath(full, folder)] = (size, digest)
    return snap


def redirect_storage(temp_folder: str) -> None:
    """
    把 vocab_book 的所有落盘出口都指到临时文件夹。

    关键点：vocab_book 里写的是 `folder: str = FOLDER`，这种默认值在**定义时**就求值了，
    只改 vocab_book.FOLDER 常量对 Store() / Store.load() 毫无影响，照样会写进真实生词本。
    所以这里把带 folder 默认值的几个函数的 __defaults__ 一起重绑到临时文件夹。
    """
    vocab_book.FOLDER = temp_folder
    vocab_book.DATA_FILE = os.path.join(temp_folder, vocab_book.Store.DATA_NAME)
    vocab_book.MARKDOWN_FILE = os.path.join(temp_folder, vocab_book.Store.MARKDOWN_NAME)

    def _repoint(func) -> None:
        defaults = getattr(func, "__defaults__", None)
        if defaults:
            func.__defaults__ = tuple(
                temp_folder if value == REAL_FOLDER else value for value in defaults
            )

    _repoint(vocab_book.Store.__init__)     # Store() → 临时目录
    _repoint(vocab_book.Store.load.__func__)  # Store.load() → 临时目录
    _repoint(vocab_book.ensure_folder)
    _repoint(vocab_book.open_folder)
    _repoint(vocab_book.timestamped_path)


def block_network() -> None:
    """把 Python 层的 socket 连接全部掐掉：这个测试里一旦想联网就直接抛异常。"""

    def _refuse(*args, **kwargs):
        raise RuntimeError("这个测试不联网：socket 连接已被 tests/test_bookmark_gui.py 禁用")

    socket.create_connection = _refuse
    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse


def click_at(widget, index: str) -> bool:
    """
    在文本控件的某个下标处，真的生成一次左键点击（走控件自己 bind 的处理函数）。
    坐标取不到（控件没显示出来 / 该行不可见）时返回 False，由调用方决定怎么办。
    """
    try:
        widget.see(index)
        widget.update()
        box = widget.bbox(index)
    except Exception:
        return False
    if not box:
        return False
    x = box[0] + max(box[2] // 2, 1)
    y = box[1] + max(box[3] // 2, 1)
    widget.event_generate("<Button-1>", x=x, y=y)
    widget.update()
    return True


def word_on_line(widget, index: str) -> str:
    """从某个下标所在的那一行里，认出那个英文单词（重点词条目行长这样：· word　▸ 展开　☆）。"""
    line = widget.get(f"{index} linestart", f"{index} lineend")
    match = re.search(r"[A-Za-z][A-Za-z'’\-]*", line)
    return match.group(0) if match else ""


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ============================================================ 主流程


def main() -> int:
    checks = Checker()
    print("收藏（☆/★）界面测试 —— 不联网；收藏只写临时文件夹")
    print(f"  项目根目录：{ROOT}")
    print(f"  真实生词本：{REAL_FOLDER}")

    # 需要盯住的真实文件夹：本项目的生词本 + 别的盘上同一份程序的生词本
    watched = [REAL_FOLDER]
    for other in OTHER_PROJECT_DIRS:
        candidate = os.path.join(other, vocab_book.FOLDER_NAME)
        if os.path.abspath(candidate) != os.path.abspath(REAL_FOLDER) and os.path.isdir(candidate):
            watched.append(candidate)
    for path in watched:
        print(f"  只读监视：{path}")
    before = {path: snapshot(path) for path in watched}

    block_network()

    temp_dir = tempfile.mkdtemp(prefix="vocab_gui_test_")
    temp_vocab = os.path.join(temp_dir, vocab_book.FOLDER_NAME)
    print(f"  临时生词本：{temp_vocab}")
    print()

    root = None
    try:
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            print(f"SKIP：这台机器起不了 tkinter 窗口（{exc}）。")
            print("看起来是没有图形界面的环境，界面测试跳过，退出码仍然是 0。")
            return 0
        root.update()

        # ---- 0. 把存储重定向到临时目录，再创建真正的主窗口（顺序不能反） ----
        print("第 1 步：把「生词本」重定向到临时目录，并起真窗口")
        redirect_storage(temp_vocab)
        import english_note_generator as gui

        app = gui.EnglishNoteApp(root)
        root.update()

        if os.path.abspath(app.vocab_book.folder) != os.path.abspath(temp_vocab):
            checks.check(
                "生词本已重定向到临时目录",
                False,
                f"实际还是 {app.vocab_book.folder}",
            )
            print("  重定向没生效，出于安全立刻停下 —— 绝不在真实生词本上做试验。")
            return 1
        checks.check("生词本已重定向到临时目录", True)
        checks.check(
            "Store 的落盘路径都在临时目录里",
            os.path.dirname(app.vocab_book.data_file) == temp_vocab
            and os.path.dirname(app.vocab_book.markdown_file) == temp_vocab,
            f"data={app.vocab_book.data_file}",
        )
        checks.check(
            "临时目录还是空的（第一次用不该凭空建文件夹）",
            not os.path.exists(app.vocab_book.data_file)
            and not os.path.exists(app.vocab_book.markdown_file),
        )
        checks.check("初始收藏数是 0", len(app.vocab_book) == 0)

        # ---- 2. 本地跑一次分析，把真实笔记渲染出来（engine.analyze 不联网） ----
        print("\n第 2 步：本地渲染笔记，找到那颗 ☆")
        app.analysis = gui.engine.analyze(SAMPLE_TEXT, app.lexicon)
        app._render_note()
        root.update()

        star_ranges = app.note_text.tag_ranges("star")
        checks.check(
            "笔记里渲染出了可点的 ☆（tag=star）",
            len(star_ranges) >= 2,
            f"tag_ranges('star') 只有 {len(star_ranges)} 个边界，说明没渲染出星星",
        )
        if len(star_ranges) < 2:
            print("  没有星星就没有可测的收藏入口，直接判失败。")
            return 1

        star_index = str(star_ranges[0])
        word = word_on_line(app.note_text, star_index)
        checks.check("能从那颗 ☆ 所在行认出单词", bool(word), f"行内容：{app.note_text.get(f'{star_index} linestart', f'{star_index} lineend')!r}")

        # ---- 3. 真的点一下 ☆ → 应该收藏进（临时的）生词本 ----
        print(f"\n第 3 步：在 ☆ 上真的点一下，期望收藏「{word}」")
        clicked = click_at(app.note_text, star_index)
        root.update()
        if clicked:
            checks.check(f"点击后「{word}」进了生词本", app.vocab_book.has(word))
        else:
            # 窗口坐标取不到（例如远程桌面下控件不可见）时退回直接调用真方法，
            # 走的还是程序自己的 _bookmark_toggle，不是重新写一遍逻辑。
            print("  （注：控件坐标取不到，退回直接调用真正的 _bookmark_toggle）")
            app._bookmark_toggle(word)
            root.update()
            checks.check(f"直接调用 _bookmark_toggle 后「{word}」进了生词本", app.vocab_book.has(word))

        data_file = app.vocab_book.data_file
        markdown_file = app.vocab_book.markdown_file
        checks.check("收藏后写出了 生词本.json", os.path.exists(data_file), data_file)
        checks.check("收藏后写出了 生词本.md", os.path.exists(markdown_file), markdown_file)
        checks.check("收藏后收藏数为 1", len(app.vocab_book) == 1, f"实际 {len(app.vocab_book)}")

        if os.path.exists(data_file):
            try:
                payload = json.loads(read_text(data_file))
                saved_words = [str(item.get("word", "")).lower()
                               for item in payload.get("words", [])]
                checks.check(
                    "生词本.json 的内容就是刚收藏的那个词",
                    payload.get("count") == 1 and word.lower() in saved_words,
                    f"count={payload.get('count')} words={saved_words}",
                )
            except (OSError, ValueError) as exc:
                checks.check("生词本.json 能正常解析", False, str(exc))

        if os.path.exists(markdown_file):
            markdown = read_text(markdown_file)
            checks.check(
                "生词本.md 是可读的那一份（标题 + 词都在）",
                markdown.startswith(vocab_book.MARKDOWN_TITLE) and word.lower() in markdown.lower(),
            )

        checks.check(
            "界面状态栏跟着显示收藏数量",
            "1 个" in app.vocab_status_var.get(),
            f"状态栏：{app.vocab_status_var.get()!r}",
        )
        checks.check(
            "笔记里的 ☆ 变成了 ★（tag=star_on）",
            len(app.note_text.tag_ranges("star_on")) >= 2
            and gui.STAR_ON in app.note_text.get("1.0", "end"),
            f"状态栏：{app.vocab_status_var.get()!r}",
        )

        # ---- 4. 再点一下 ★ → 应该取消收藏 ----
        print("\n第 4 步：在 ★ 上再点一下，期望取消收藏")
        on_ranges = app.note_text.tag_ranges("star_on")
        if len(on_ranges) >= 2:
            on_index = str(on_ranges[0])
            clicked_back = click_at(app.note_text, on_index)
            root.update()
            if not clicked_back:
                print("  （注：控件坐标取不到，退回直接调用真正的 _bookmark_toggle）")
                app._bookmark_toggle(word)
                root.update()
        else:
            checks.check("笔记里出现了 ★", False, "找不到 tag=star_on 的星星")
            app._bookmark_toggle(word)
            root.update()

        checks.check("再点一下 ★ 就取消收藏", not app.vocab_book.has(word))
        checks.check("取消后收藏数回到 0", len(app.vocab_book) == 0, f"实际 {len(app.vocab_book)}")
        if os.path.exists(data_file):
            payload = json.loads(read_text(data_file))
            checks.check(
                "生词本.json 里的 count 也回到 0（真的落盘了）",
                payload.get("count") == 0,
                f"count={payload.get('count')}",
            )
        checks.check(
            "状态栏回到「空」",
            "空" in app.vocab_status_var.get(),
            f"状态栏：{app.vocab_status_var.get()!r}",
        )

        # ---- 5. 直接用真方法再走两轮（顺带验证大小写不敏感 / 重复收藏不会变两条） ----
        print("\n第 5 步：直接调用 _bookmark_toggle，验证开关是幂等的")
        app._bookmark_toggle("paradigm")
        root.update()
        checks.check("_bookmark_toggle 能收藏 paradigm", app.vocab_book.has("paradigm"))
        app._bookmark_toggle("Paradigm")
        root.update()
        checks.check(
            "_bookmark_toggle 大小写不敏感，再调一次就取消",
            not app.vocab_book.has("paradigm"),
            f"还剩 {sorted(app.vocab_book.words())}",
        )

        app._bookmark("paradigm")
        app._bookmark("paradigm")
        root.update()
        checks.check(
            "同一个词重复收藏只会留一条",
            len(app.vocab_book) == 1,
            f"实际 {len(app.vocab_book)} 条",
        )
        app._unbookmark("paradigm")
        root.update()
        checks.check("_unbookmark 之后生词本为空", len(app.vocab_book) == 0)
        checks.check(
            "收藏只写在临时目录里（临时 json 还在）",
            os.path.exists(data_file) and os.path.dirname(data_file) == temp_vocab,
            data_file,
        )

        # ---- 6. 最后：真东西一个字节都没动 ----
        print("\n第 6 步：比对真实生词本的快照")
        after = {path: snapshot(path) for path in watched}
        for path in watched:
            checks.check(
                f"没被动过：{path}",
                before[path] == after[path],
                f"测试前 {before[path]}　测试后 {after[path]}",
            )
        existed_before = before[REAL_FOLDER] is not None
        checks.check(
            f"项目根目录下的 生词本 文件夹{'仍然存在' if existed_before else '仍然不存在'}（没被创建也没被删）",
            os.path.isdir(REAL_FOLDER) == existed_before,
            f"{REAL_FOLDER} 现在存在？{os.path.exists(REAL_FOLDER)}",
        )

        print()
        if checks.failed:
            print(f"✗ 失败 {len(checks.failed)}/{checks.total} 项：")
            for name in checks.failed:
                print(f"    · {name}")
            return 1
        print(f"✓ 全部通过（共 {checks.total} 项检查）")
        print(f"  收藏全程只写在：{temp_vocab}")
        print("  真实生词本：一个字节都没动。")
        return 0

    except Exception:
        print("\n✗ 测试中途出错（下面就是真正的原因）：")
        traceback.print_exc()
        return 1
    finally:
        # 关窗口（不 mainloop，destroy 干净利落）+ 删掉这次测试的临时收藏
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
