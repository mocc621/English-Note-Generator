# -*- coding: utf-8 -*-
"""
diagnose_translation.py —— 翻译失败诊断小工具（不联网查词，只测翻译接口）

用法（在项目根目录）：
    1) 把翻译失败的那段英文另存为 fail.txt（放同一个文件夹，直接粘贴原文即可）
    2) 运行：  py diagnose_translation.py
    3) 如果想连着试几次看是不是偶发： py diagnose_translation.py 3

它做的事和程序里完全一样（同一套分块规则、同一个接口），所以：
    · 这里全部成功 → 你那次多半是临时抽风（额度/网络抖动），重试即可
    · 这里某一块失败 → 会把接口原话打印出来，就是它失败的真实原因
"""

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import translator_api as trans

TEXT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fail.txt")


def probe_chunk(chunk: str, timeout: int = 20) -> tuple:
    """对一小块文本发一次和程序一模一样的请求，返回 (是否成功, 说明)。"""
    params = urllib.parse.urlencode(
        {"q": chunk, "langpair": "en|zh-CN", "de": trans.MYMEMORY_CONTACT}
    )
    request = urllib.request.Request(
        f"{trans.MYMEMORY_API}?{params}", headers={"User-Agent": trans._UA}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
        import json
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {exc.code} {exc.reason}　接口原话：{body}"
    except urllib.error.URLError as exc:
        return False, f"连不上（{exc.reason}）—— 网络/DNS/代理问题"
    except TimeoutError:
        return False, "超时（20 秒）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    details = str(data.get("responseDetails") or "").strip()
    if data.get("quotaFinished"):
        return False, f"额度已用完 quotaFinished=True　{details[:120]}"
    if details and "MYMEMORY WARNING" in details.upper():
        return False, f"额度警告：{details[:120]}"
    piece = str((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not piece:
        return False, f"接口没返回译文　responseData={data.get('responseData')!r}"[:200]
    if piece.lower() == chunk.lower():
        return False, "接口把英文原样退回来了（通常就是额度用尽/被限流）"
    return True, piece[:60]


def main() -> None:
    rounds = 1
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        rounds = max(1, int(sys.argv[1]))

    if not os.path.exists(TEXT_FILE):
        print(f"没找到 {TEXT_FILE}")
        print("请把翻译失败的英文粘进这个文件（UTF-8），存成 fail.txt，再运行一次。")
        return

    with open(TEXT_FILE, "r", encoding="utf-8") as handle:
        text = handle.read()
    text = text.strip()
    if not text:
        print("fail.txt 是空的。")
        return

    from notebook_engine import split_paragraphs  # 用和程序一样的分段规则

    paragraphs = [p for p in split_paragraphs(text) if p and p.strip()]
    print(f"读入 {len(paragraphs)} 段（共 {len(text.encode('utf-8'))} 字节）")
    print(f"接口：{trans.MYMEMORY_API}")
    print("-" * 70)

    for round_index in range(1, rounds + 1):
        if rounds > 1:
            print(f"第 {round_index} 轮：")
        for index, paragraph in enumerate(paragraphs, 1):
            chunks = trans._auto_chunk(paragraph)
            print(f"第 {index} 段（{len(paragraph.encode('utf-8'))} 字节，切成 {len(chunks)} 块）")
            for chunk_index, chunk in enumerate(chunks, 1):
                started = time.time()
                ok, detail = probe_chunk(chunk)
                flag = "成功" if ok else "失败"
                print(f"    第 {chunk_index} 块 [{time.time() - started:5.2f}s] {flag}：{detail}")
                if not ok:
                    print(f"      这块的原文是：{chunk[:100]}")
                    print()
                    print(">>> 结论：这段文本本身在接口那边被拒了，原因见上面那行。")
                    print(">>> 如果写的是 HTTP 429 / 额度 / MYMEMORY WARNING：等额度重置，")
                    print(">>> 或在「⚙ 设置」里选「百度翻译 API」并填 AppID + 密钥（免费额度更高）。")
                    return
        print("-" * 70)

    print("全部成功。")
    print("说明接口和网络现在都正常 —— 你那次失败大概率是：")
    print("  · 短时间请求太密被临时限流，或当天免费额度短暂耗尽（过一会儿/第二天就好）")
    print("  · 当时网络抖动、或翻译请求超时")
    print("处理办法：在失败的那一段点一下红色的「[翻译失败，点击重试 · 第 N 段]」即可单独重翻。")


if __name__ == "__main__":
    main()
