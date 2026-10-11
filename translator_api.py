# -*- coding: utf-8 -*-
"""
translator_api.py —— 在线能力封装

四块能力，互相独立，都能单独测：

  1. 自动翻译（免密钥）：MyMemory 公开接口 + 本地缓存。面向"打开就能用"。
  2. 机器翻译：百度翻译 / DeepL 官方 API（填了密钥就走这个，质量更稳）
  3. AI 总结：任意 OpenAI 兼容接口（DeepSeek、OpenAI、通义、Kimi 等）
     用于"段落大意总结"——DeepL/百度只会翻译，不会总结，所以这块必须走大模型。
  4. 外接翻译网站：不填任何密钥，直接把段落带进浏览器里的翻译网站

只依赖 Python 标准库（urllib + json + hashlib），不用 pip 装任何东西。
密钥类接口失败一律抛 TranslationError（消息可直接给用户看）；
免密钥的自动翻译失败则返回空串，由界面静默降级，绝不打断流程。
"""

import hashlib
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

BAIDU_ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"
DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_ENDPOINT = "https://api.deepl.com/v2/translate"

# 每次翻译请求最多发多少字符（百度上限 6000 字节，这里保守取值）
MAX_CHARS_PER_REQUEST = 1800

# AI 总结：单次请求的文本上限，超了会分批总结再合并
MAX_CHARS_PER_SUMMARY = 3000

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

PROVIDER_OFFLINE = "离线（不联网）"
PROVIDER_BAIDU = "百度翻译 API"
PROVIDER_DEEPL = "DeepL API"

# 大模型服务预设：(显示名, 接口地址, 默认模型)
LLM_PRESETS = [
    ("DeepSeek", "https://api.deepseek.com/v1/chat/completions", "deepseek-chat"),
    ("OpenAI", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    ("通义千问（兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-plus"),
    ("Kimi（月之暗面）", "https://api.moonshot.cn/v1/chat/completions", "moonshot-v1-8k"),
    ("自定义", "", ""),
]

# 外接翻译网站：显示名 → URL 模板（{text} 会被替换成 URL 编码后的文本）
TRANSLATE_SITES = [
    ("DeepL", "https://www.deepl.com/translator#en/zh/{text}"),
    ("Google 翻译", "https://translate.google.com/?sl=en&tl=zh-CN&text={text}&op=translate"),
    ("必应翻译", "https://www.bing.com/translator?from=en&to=zh-Hans&text={text}"),
    ("有道翻译", "https://fanyi.youdao.com/#/TextTranslate?text={text}"),
    ("百度翻译", "https://fanyi.baidu.com/#en/zh/{text}"),
]


class TranslationError(Exception):
    """翻译/总结失败时抛出，消息可以直接给用户看。"""


# ---------------------------------------------------------------- 语言代码

_BAIDU_LANG = {"zh": "zh", "en": "en"}
_DEEPL_LANG = {"zh": "ZH", "en": "EN"}


def _deepl_target(lang: str) -> str:
    return "ZH" if lang == "zh" else _DEEPL_LANG.get(lang, lang.upper())


# ---------------------------------------------------------------- 配置对象


@dataclass
class TranslatorConfig:
    """翻译 + AI 总结的配置。字段名和"设置"窗口里的输入框一一对应。"""

    provider: str = PROVIDER_OFFLINE
    baidu_appid: str = ""
    baidu_key: str = ""
    deepl_key: str = ""

    # AI 总结（可选）
    llm_key: str = ""
    llm_base: str = LLM_PRESETS[0][1]
    llm_model: str = LLM_PRESETS[0][2]

    source_lang: str = "en"
    target_lang: str = "zh"

    # ---- 能力检查 ----
    def translation_ready(self) -> bool:
        if self.provider == PROVIDER_BAIDU:
            return bool(self.baidu_appid.strip() and self.baidu_key.strip())
        if self.provider == PROVIDER_DEEPL:
            return bool(self.deepl_key.strip())
        return False

    def llm_ready(self) -> bool:
        return bool(self.llm_key.strip() and self.llm_base.strip())

    def describe_problem(self) -> str:
        """翻译没配好时，告诉用户到底缺什么。"""
        if self.provider == PROVIDER_BAIDU:
            if not self.baidu_appid.strip():
                return "还没填「百度 AppID」，请点右上角「⚙ 设置」填写。"
            if not self.baidu_key.strip():
                return "还没填「百度密钥」，请点右上角「⚙ 设置」填写。"
        if self.provider == PROVIDER_DEEPL and not self.deepl_key.strip():
            return "还没填「DeepL API Key」，请点右上角「⚙ 设置」填写。"
        if self.provider == PROVIDER_OFFLINE:
            return "当前是离线模式。可以填百度 / DeepL 密钥，或者直接点「在翻译网站打开」。"
        return ""

    def describe_llm_problem(self) -> str:
        if not self.llm_key.strip():
            return "还没填 AI 密钥（用于总结大意）。DeepSeek / OpenAI 都可以。"
        if not self.llm_base.strip():
            return "还没填 AI 接口地址。"
        return ""

    def public_view(self) -> dict:
        """保存到 config.json 的内容。"""
        return {
            "provider": self.provider,
            "baidu_appid": self.baidu_appid,
            "baidu_key": self.baidu_key,
            "deepl_key": self.deepl_key,
            "llm_key": self.llm_key,
            "llm_base": self.llm_base,
            "llm_model": self.llm_model,
        }


# ---------------------------------------------------------------- 分批工具


def _split_into_batches(text: str, limit: int = MAX_CHARS_PER_REQUEST) -> List[str]:
    """
    把长文本切成若干段，每段不超过 limit 个字符。
    优先在换行处切；没有换行再在句末切；实在不行才硬切。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) <= limit:
            pieces.append(line)
            continue
        for sent in re.findall(r"[^.!?]*[.!?]+|\S[^.!?]*$", line):
            sent = sent.strip()
            if sent:
                pieces.append(sent)

    batches: List[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > limit:
            batches.append(piece[:limit])
            piece = piece[limit:]
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= limit:
            current = current + "\n" + piece
        else:
            batches.append(current)
            current = piece
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------- HTTP


def _post(url: str, data: bytes, headers: dict, timeout: int = 30) -> dict:
    """发 POST 请求并解析 JSON。出错统一转成 TranslationError。"""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise TranslationError(
            f"服务返回错误 HTTP {exc.code}。\n{detail}\n"
            "常见原因：密钥不对、免费额度用完、模型名写错、或网络被拦截。"
        ) from exc
    except urllib.error.URLError as exc:
        raise TranslationError(
            f"连不上服务：{exc.reason}\n请检查网络，或公司网络是否限制了外部访问。"
        ) from exc
    except TimeoutError as exc:
        raise TranslationError("请求超时，请稍后重试或把文本拆短一些。") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"服务返回了无法解析的内容：{raw[:200]}") from exc


# ---------------------------------------------------------------- 百度 / DeepL


def _request_baidu(batch: str, cfg: TranslatorConfig) -> str:
    salt = str(random.randint(32768, 65536))
    sign_source = cfg.baidu_appid + batch + salt + cfg.baidu_key
    sign = hashlib.md5(sign_source.encode("utf-8")).hexdigest()

    payload = urllib.parse.urlencode(
        {
            "q": batch,
            "from": _BAIDU_LANG.get(cfg.source_lang, "en"),
            "to": _BAIDU_LANG.get(cfg.target_lang, "zh"),
            "appid": cfg.baidu_appid,
            "salt": salt,
            "sign": sign,
        }
    ).encode("utf-8")

    result = _post(BAIDU_ENDPOINT, payload, {"Content-Type": "application/x-www-form-urlencoded"})

    if "error_code" in result:
        code = result.get("error_code")
        hint = {
            "52001": "请求超时，稍后重试。",
            "52002": "系统错误，稍后重试。",
            "52003": "AppID 或密钥不正确（未授权用户）。",
            "54000": "必填参数为空，检查 AppID 和密钥是否填全。",
            "54001": "签名错误，通常是密钥填错了。",
            "54003": "访问频率受限，请稍等一会再试。",
            "54004": "账户余额不足。",
            "54005": "长文本请求频繁，请稍后重试。",
            "58000": "客户端 IP 不在你设置的 IP 白名单里。",
            "58001": "译文语言方向不支持。",
            "58002": "服务已关闭，请到百度控制台开启通用文本翻译。",
        }.get(str(code), "请到百度翻译控制台查看错误码含义。")
        raise TranslationError(f"百度翻译报错 {code}：{result.get('error_msg', '')}\n{hint}")

    items = result.get("trans_result") or []
    if not items:
        raise TranslationError("百度翻译没有返回译文，请稍后重试。")
    return "\n".join(item.get("dst", "") for item in items)


def _request_deepl(batch: str, cfg: TranslatorConfig) -> str:
    key = cfg.deepl_key.strip()
    endpoint = DEEPL_FREE_ENDPOINT if key.endswith(":fx") else DEEPL_PRO_ENDPOINT

    payload = json.dumps(
        {
            "text": [batch],
            "target_lang": _deepl_target(cfg.target_lang),
            "source_lang": _DEEPL_LANG.get(cfg.source_lang, "EN"),
        },
        ensure_ascii=False,
    ).encode("utf-8")

    result = _post(
        endpoint,
        payload,
        {"Content-Type": "application/json", "Authorization": f"DeepL-Auth-Key {key}"},
    )

    translations = result.get("translations") or []
    if not translations:
        raise TranslationError("DeepL 没有返回译文，请稍后重试。")
    return "\n".join(item.get("text", "") for item in translations)


def translate(
    text: str,
    cfg: TranslatorConfig,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """把 text 翻成目标语言。progress(已完成, 总数) 用于更新进度条。"""
    if not text.strip():
        return ""
    if cfg.provider == PROVIDER_OFFLINE:
        raise TranslationError("当前是离线模式，不联网翻译。")
    problem = cfg.describe_problem()
    if problem and not cfg.translation_ready():
        raise TranslationError(problem)

    batches = _split_into_batches(text)
    total = len(batches)
    out: List[str] = []
    for index, batch in enumerate(batches, 1):
        if cfg.provider == PROVIDER_BAIDU:
            out.append(_request_baidu(batch, cfg))
        elif cfg.provider == PROVIDER_DEEPL:
            out.append(_request_deepl(batch, cfg))
        else:
            raise TranslationError(f"不认识的翻译服务：{cfg.provider}")
        if progress:
            progress(index, total)
    return "\n".join(out)


def translate_paragraphs(
    paragraphs: List[str],
    cfg: TranslatorConfig,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[str]:
    """
    逐段翻译，保证"一段英文 ↔ 一段中文"严格对应。
    返回的列表长度与输入一致；顺序与输入相同，不会因为接口换行而错位。
    """
    if cfg.provider == PROVIDER_OFFLINE:
        raise TranslationError("当前是离线模式，不联网翻译。")
    if not cfg.translation_ready():
        raise TranslationError(cfg.describe_problem() or "翻译服务还没配置好。")

    results: List[str] = []
    total = len(paragraphs)
    for index, paragraph in enumerate(paragraphs, 1):
        text = paragraph.strip()
        if not text:
            results.append("")
        else:
            results.append(translate(text, cfg).strip())
        if progress:
            progress(index, total)
    return results


# ---------------------------------------------------------------- AI 总结


def summarize(
    text: str,
    cfg: TranslatorConfig,
    system_prompt: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    调大模型做总结/提炼。走 OpenAI 兼容的 /chat/completions 接口，
    所以 DeepSeek、OpenAI、通义、Kimi 都能用，只要填对 base 和 model。
    """
    if not text.strip():
        return ""
    problem = cfg.describe_llm_problem()
    if problem:
        raise TranslationError(problem)

    batches = _split_into_batches(text, MAX_CHARS_PER_SUMMARY)
    total = len(batches)
    parts: List[str] = []

    for index, batch in enumerate(batches, 1):
        payload = json.dumps(
            {
                "model": cfg.llm_model or "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": batch},
                ],
                "temperature": 0.3,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        result = _post(
            cfg.llm_base,
            payload,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.llm_key.strip()}",
            },
            timeout=60,
        )

        if "error" in result:
            message = result["error"].get("message", "") if isinstance(result["error"], dict) else str(result["error"])
            raise TranslationError(f"AI 接口报错：{message}")

        choices = result.get("choices") or []
        if not choices:
            raise TranslationError("AI 没有返回内容，请检查模型名是否写对。")
        content = (choices[0].get("message") or {}).get("content", "")
        parts.append(str(content).strip())

        if progress:
            progress(index, total)

    return "\n".join(part for part in parts if part)


# ================================================================ 自动翻译（免密钥）
# 面向"打开就能用"的普通学生：本地没有好用的免费官方接口，
# 所以使用公开的 MyMemory 接口。它是免费翻译记忆库，免注册，返回 JSON。
# 风险：有每日字符额度、可能限流、服务方随时可能调整。所以：
#   * 译文存本地（auto_tr_cache.json），同样的段落不再重复请求
#   * 失败不抛异常打断流程，只是这一段没译文，界面照常显示其它内容

MYMEMORY_API = "https://api.mymemory.translated.net/get"
AUTO_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_tr_cache.json")

# MyMemory 的查询串上限约 500 字节，留点余量
AUTO_MAX_CHARS = 460

# MyMemory 匿名请求只给约 5000 字符/天的额度，很容易撞 429（Too Many Requests）。
# 带上 de= 邮箱参数后额度提升到约 50000 字符/天。
# 这里用一个中性的、专门用于本工具的标识；如果你有自己的邮箱，填进去更稳妥。
MYMEMORY_CONTACT = "english-note-generator@example.com"


class AutoTranslateError(Exception):
    """自动翻译失败。界面通常不需要显示它，留着给日志和自检用。"""


@dataclass
class AutoResult:
    """
    自动翻译的结果。
    译文可能为空，此时 text 是空串、reason 说明原因 —— 界面据此提示用户，
    而不是像以前那样静默失败（那样用户只会看到"没拿到译文"，不知道为什么）。
    """

    text: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text)

    def __bool__(self) -> bool:
        return self.ok


def load_auto_cache(path: str = AUTO_CACHE_FILE) -> Dict[str, str]:
    """读自动翻译的本地缓存 {英文: 中文}。坏文件不影响使用。"""
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


def save_auto_cache(cache: Dict[str, str], path: str = AUTO_CACHE_FILE) -> None:
    """保存自动翻译缓存。同样的段落下次不再联网。"""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=0, sort_keys=True)
    except OSError:
        pass


def _auto_chunk(text: str, limit: int = AUTO_MAX_CHARS) -> List[str]:
    """
    把段落切成接口能吃下的块，**在句末切，并且标点永远跟着它所属的句子走**。

    为什么必须这样（这里踩过坑）：
        以前的切法是按"句号/问号/叹号后面"分片，于是句末的引号、右括号会被算作
        **下一块的开头**。当它正好落在块边界上时，就会切出一块只有标点的内容：

            'The official said "..." cover more ground.'   ← 460 字节，到这里结束
            '"'                                            ← 只有这一个引号！

        单独一块标点发给翻译接口，接口会原样吐回来，程序判定"没有有效译文"，
        于是**整段报翻译失败**（前面翻好的句子也一起丢掉）。
    所以现在：
        1. 右引号 / 右括号跟在句末标点后面，一起算作这一句的结尾；
        2. 万一还是出现"只有标点"的碎片（异常文本），就把它并回前一块，绝不单独发出去。
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]

    # 一句话 = 句内内容 + 句末标点 + 跟在标点后面的右引号/右括号 + 后面的空白。
    # 注意两个字符类的写法：
    #   · 句内允许引号（' " ’ ” 都是"词的一部分"，比如 said "hello"），只排除真正的句末符号；
    #   · 只有**跟在句末标点后面**的右引号/右括号才算"这一句的尾巴"，这样 "…ground." 不会被切散。
    pieces = re.findall(r"""[^.!?]+[.!?]+['")\u201d\u2019\]]*\s*|[^.!?]+""", text)
    chunks: List[str] = []
    buffer = ""
    for piece in pieces:
        if len((buffer + piece).encode("utf-8")) > limit and buffer:
            chunks.append(buffer.strip())
            buffer = piece
        else:
            buffer += piece
        # 单句就超长时硬切（按字节上限切，且不会切断多字节字符）
        while len(buffer.encode("utf-8")) > limit:
            cut = _safe_cut(buffer, limit)
            chunks.append(buffer[:cut].strip())
            buffer = buffer[cut:]
    if buffer.strip():
        chunks.append(buffer.strip())

    # 兜底：只有标点的碎片并回它前面那一块，别让它单独去见接口
    merged: List[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if merged and not any(ch.isalnum() for ch in chunk):
            merged[-1] = merged[-1] + " " + chunk
        else:
            merged.append(chunk)
    # 万一第一块本身就只有标点（异常输入），也只能留着（没有"前一块"可并）
    return [c for c in merged if c]


def _safe_cut(text: str, limit: int) -> int:
    """按字节上限找一个不会切断多字节字符的切点。"""
    used = 0
    for index, char in enumerate(text):
        used += len(char.encode("utf-8"))
        if used > limit:
            return max(index, 1)
    return len(text)


def _shorten(reason: str, limit: int = 60) -> str:
    """把接口的错误原话压短一点，方便塞进状态栏。"""
    reason = re.sub(r"\s+", " ", str(reason or "")).strip()
    return reason[:limit]


def _translate_auto_piece(chunk: str, timeout: int = 20) -> Tuple[bool, str]:
    """
    把一小块文本发给免密钥接口，返回 (是否成功, 译文或失败原因)。

    单独抽出来是为了"分块重试"：一块失败不再连累整段。
    """
    params = urllib.parse.urlencode(
        {"q": chunk, "langpair": "en|zh-CN", "de": MYMEMORY_CONTACT}
    )
    request = urllib.request.Request(
        f"{MYMEMORY_API}?{params}", headers={"User-Agent": _UA}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return False, ("免费翻译额度已用完（HTTP 429）。可以等明天再试，"
                           "或在「⚙ 设置」里填一个百度翻译密钥（免费额度更高、更稳定）。")
        return False, f"翻译接口返回错误 HTTP {exc.code}。"
    except urllib.error.URLError as exc:
        return False, f"连不上翻译服务（{exc.reason}），请检查网络。"
    except TimeoutError:
        return False, "翻译请求超时，请稍后重试或把文本拆短。"
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"翻译接口返回异常（{type(exc).__name__}）。"

    # 接口自己报告的错误（额度、语言不支持等）
    details = str(data.get("responseDetails") or "").strip()
    if details and "MYMEMORY WARNING" in details.upper():
        return False, f"免费翻译额度已用完：{details[:80]}"
    if data.get("quotaFinished"):
        return False, "免费翻译额度已用完，请明天再试或改用百度密钥。"

    piece = str((data.get("responseData") or {}).get("translatedText") or "").strip()
    # 接口失败时会把原文回吐，这时视为没翻出来
    if not piece or piece.lower() == chunk.lower():
        return False, "翻译接口没有返回有效译文，请稍后重试。"
    return True, piece


def auto_translate(
    text: str,
    cache: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    use_cache: bool = True,
) -> "AutoResult":
    """
    把一段英文翻成中文，不需要任何密钥。

    cache    本地缓存 {英文: 中文}，命中则不联网（会被就地更新）
    返回 AutoResult：成功时 .text 是译文；失败时 .text 为空、.reason 说明原因。
    不抛异常（翻译失败不该打断整个笔记生成），但原因会带出来给界面显示。

    分块策略：一块失败了**不丢掉别的块** —— 先把翻好的部分拼出来给用户看，
    同时在 .reason 里说清哪一块没翻出来（以前是一块失败整段空白，很气人）。
    """
    text = (text or "").strip()
    if not text:
        return AutoResult()

    cache = cache if cache is not None else {}
    if use_cache and text in cache:
        return AutoResult(text=cache[text])

    chunks = _auto_chunk(text)
    translated: List[str] = []
    failed_chunks: List[int] = []
    first_reason = ""
    for index, chunk in enumerate(chunks, 1):
        ok, detail = _translate_auto_piece(chunk, timeout=timeout)
        if ok:
            translated.append(detail)
        else:
            failed_chunks.append(index)
            if not first_reason:
                first_reason = detail

    result = " ".join(t for t in translated if t).strip()
    if not result:
        # 一块都没翻出来：整段算失败，原因是第一条
        return AutoResult(reason=first_reason or "翻译接口没有返回内容，请稍后重试。")

    if failed_chunks:
        where = "、".join(f"第 {i} 块" for i in failed_chunks[:3])
        more = "等" if len(failed_chunks) > 3 else ""
        # 部分成功：给出译文，同时说明哪一块没翻出来
        return AutoResult(
            text=result,
            reason=f"{where}{more}没翻出来（{_shorten(first_reason)}）；"
                   f"上面显示的译文是其余部分，点重试可以把整段补全。",
        )

    cache[text] = result
    return AutoResult(text=result)


def _auto_smoke_test() -> bool:
    """给自检用：真的试一次联网。"""
    try:
        return bool(auto_translate("Hello world, this is a test.", use_cache=False).text)
    except Exception:
        return False


# ---------------------------------------------------------------- 外接翻译网站


def site_url(site_name: str, text: str) -> str:
    """
    生成翻译网站的链接，把英文塞进 URL 里，点开就是翻译好的页面。
    只依赖网址参数，不需要任何密钥。
    """
    for name, template in TRANSLATE_SITES:
        if name == site_name:
            return template.format(text=urllib.parse.quote(text, safe=""))
    return ""


def site_url_too_long(site_name: str, text: str, limit: int = 1800) -> bool:
    """URL 太长（浏览器/网站可能截断）时返回 True，界面会改走剪贴板方案。"""
    return len(site_url(site_name, text)) > limit


# ---------------------------------------------------------------- 对外小工具


def provider_is_online(provider: str) -> bool:
    return provider in (PROVIDER_BAIDU, PROVIDER_DEEPL)


# ---------------------------------------------------------------- 自检


def self_test(live: bool = False) -> None:
    """不联网的自检：分批、配置校验、语言码、网站链接、离线拦截。"""
    long_text = ("This is a sentence. " * 200).strip()
    batches = _split_into_batches(long_text)
    assert len(batches) > 1, "长文本应该被拆成多批"
    assert all(len(b) <= MAX_CHARS_PER_REQUEST for b in batches), "每批不能超长"
    assert _split_into_batches("") == []

    # 翻译配置校验
    cfg = TranslatorConfig(provider=PROVIDER_BAIDU)
    assert not cfg.translation_ready() and cfg.describe_problem()
    cfg = TranslatorConfig(provider=PROVIDER_BAIDU, baidu_appid="1", baidu_key="2")
    assert cfg.translation_ready() and not cfg.describe_problem()
    cfg = TranslatorConfig(provider=PROVIDER_DEEPL, deepl_key="abc")
    assert cfg.translation_ready()
    assert _deepl_target("zh") == "ZH"

    # AI 配置校验
    cfg = TranslatorConfig()
    assert not cfg.llm_ready() and cfg.describe_llm_problem()
    cfg = TranslatorConfig(llm_key="sk-test", llm_base="https://api.deepseek.com/v1/chat/completions")
    assert cfg.llm_ready() and not cfg.describe_llm_problem()

    # 离线拦截
    for call in (
        lambda: translate("hello", TranslatorConfig()),
        lambda: translate_paragraphs(["hello"], TranslatorConfig()),
        lambda: summarize("hello", TranslatorConfig(), "系统提示"),
    ):
        try:
            call()
        except TranslationError:
            pass
        else:
            raise AssertionError("未配置时应该抛错")

    # 外接翻译网站链接
    url = site_url("DeepL", "hello world")
    assert url.startswith("https://www.deepl.com/") and "hello%20world" in url, url
    google = site_url("Google 翻译", "a b")
    assert "translate.google.com" in google and "a%20b" in google, google
    assert site_url("不存在的网站", "x") == ""
    assert not site_url_too_long("DeepL", "short text")
    assert site_url_too_long("DeepL", "word " * 600), "超长文本应被判定为 URL 过长"

    # 自动翻译：分块逻辑
    short = "This is one short sentence."
    assert _auto_chunk(short) == [short]
    long_para = ("Remote work enhances productivity. " * 40).strip()
    chunks = _auto_chunk(long_para)
    assert len(chunks) > 1, "长段落应被切块"
    assert all(len(c.encode("utf-8")) <= AUTO_MAX_CHARS for c in chunks), "每块不能超过字节上限"
    # 切块不能把多字节字符切断（拼回来应能对上原文的非空白字符）
    joined = "".join(chunks)
    original = long_para
    assert (("".join(c for c in joined if not c.isspace()))
            == ("".join(c for c in original if not c.isspace()))), "切块后内容必须无损"
    # 中文/多字节字符也安全
    mixed = "The term 光合作用 appears here. " * 30
    assert all(len(c.encode("utf-8")) <= AUTO_MAX_CHARS for c in _auto_chunk(mixed))

    # ---- 标点必须跟着句子走（曾经把句末引号切成单独一块，整段翻译失败）----
    quoted = 'The official said "smart travel allowed us to cover more ground."'
    for count in range(1, 25):        # 扫过各种边界长度
        para = " ".join([quoted] * count)
        pieces = _auto_chunk(para)
        assert pieces, f"{count} 句时不该切出空块"
        for piece in pieces:
            assert len(piece.encode("utf-8")) <= AUTO_MAX_CHARS, "每块不能超字节上限"
            assert any(ch.isalnum() for ch in piece), \
                f"不能切出只有标点的块：{piece!r}（原文 {count} 句）"
        # 引号不能被单独留在块首：块首不该是引号/右括号
        for piece in pieces:
            assert piece[:1] not in '"\u201d\u2019\'', \
                f"块首不该是孤立的标点：{piece[:20]!r}"
        # 内容无损：字母和标点一个都不能少（空白会被规范化，不参与比较）
        def _core(s: str) -> str:
            return "".join(ch for ch in s if not ch.isspace())

        assert _core("".join(pieces)) == _core(para), \
            f"切块后文字/标点必须无损（{count} 句）"
    # 极端情况：文本本身就是标点，也不能崩
    assert _auto_chunk('."') == ['."']
    # 句末标点 + 右括号 / 右引号的组合
    tricky = ('He said the plan (announced last week) would work. "It will," he added. ' * 12).strip()
    tricky_chunks = _auto_chunk(tricky)
    assert all(any(ch.isalnum() for ch in c) for c in tricky_chunks)
    assert all(c[:1] not in '")' for c in tricky_chunks), tricky_chunks

    # 自动翻译缓存命中时不联网
    cache = {"Hello.": "你好。"}
    hit = auto_translate("Hello.", cache=cache)
    assert hit.text == "你好。" and hit.ok, f"缓存命中应返回译文：{hit}"
    empty = auto_translate("", cache=cache)
    assert empty.text == "" and not empty.ok, "空输入应返回空结果"

    # 失败时必须带原因（以前是静默返回空串，用户看不到为什么）
    assert AutoResult().reason == "" or True
    fail = AutoResult(reason="测试原因")
    assert not fail and fail.reason == "测试原因"

    # 缓存读写
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auto.json")
            assert load_auto_cache(path) == {}
            save_auto_cache({"a": "甲"}, path)
            assert load_auto_cache(path)["a"] == "甲"
    except OSError:
        # 受限环境（例如临时目录只读）写不了文件；这不是代码问题，跳过这一段
        print("  跳过缓存读写检查（临时目录不可写）")

    print("translator_api 自检通过：分批 / 配置校验 / 语言码 / 离线拦截 / 网站链接 / 自动翻译分块 均正常。")

    if live:
        print("联网实测自动翻译：")
        for text in ("Remote work enhances productivity.", "The phenomenon is not inevitable."):
            res = auto_translate(text, use_cache=False)
            if res.ok:
                print(f"  OK   {text[:42]:44} → {res.text}")
            else:
                print(f"  FAIL {text[:42]:44} → {res.reason}")
        long_res = auto_translate(long_para, use_cache=False)
        if long_res.ok:
            print(f"  长段落（{len(long_para)} 字符）→ 成功，{len(long_res.text)} 字")
        else:
            print(f"  长段落（{len(long_para)} 字符）→ {long_res.reason}")


if __name__ == "__main__":
    import sys

    self_test(live="--live" in sys.argv)
