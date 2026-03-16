# -*- coding: utf-8 -*-
"""
Telegram 发送提醒服务

职责：
1. 通过 Telegram Bot API 发送 文本消息
2. 通过 Telegram Bot API 发送 图片消息
"""

import logging
import re
import time
from typing import Optional

import requests

from src.config import Config

logger = logging.getLogger(__name__)


class TelegramSender:
    def __init__(self, config: Config):
        """
        初始化 Telegram 配置

        Args:
            config: 配置对象
        """
        self._telegram_config = {
            "bot_token": getattr(config, "telegram_bot_token", None),
            "chat_id": getattr(config, "telegram_chat_id", None),
            "message_thread_id": getattr(config, "telegram_message_thread_id", None),
        }

    def _is_telegram_configured(self) -> bool:
        """检查 Telegram 配置是否完整"""
        return bool(
            self._telegram_config["bot_token"] and self._telegram_config["chat_id"]
        )

    def send_to_telegram(self, content: str) -> bool:
        """
        推送消息到 Telegram 机器人

        Telegram Bot API 格式：
        POST https://api.telegram.org/bot<token>/sendMessage
        {
            "chat_id": "xxx",
            "text": "消息内容",
            "parse_mode": "Markdown"
        }

        Args:
            content: 消息内容（Markdown 格式）

        Returns:
            是否发送成功
        """
        if not self._is_telegram_configured():
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        bot_token = self._telegram_config["bot_token"]
        chat_id = self._telegram_config["chat_id"]
        message_thread_id = self._telegram_config.get("message_thread_id")

        try:
            # Telegram API 端点
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            # Telegram 消息最大长度 4096 字符
            max_length = 4096

            if len(content) <= max_length:
                # 单条消息发送
                return self._send_telegram_message(
                    api_url, chat_id, content, message_thread_id
                )
            else:
                # 分段发送长消息
                return self._send_telegram_chunked(
                    api_url, chat_id, content, max_length, message_thread_id
                )

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return False

    def _send_telegram_message(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
    ) -> bool:
        """Send a single Telegram message with exponential backoff retry."""
        telegram_text = self._convert_to_telegram_html(text)

        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(api_url, json=payload, timeout=10)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as e:
                if attempt < max_retries:
                    delay = 2**attempt
                    logger.warning(
                        f"Telegram request failed (attempt {attempt}/{max_retries}): {e}, "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    f"Telegram request failed after {max_retries} attempts: {e}"
                )
                return False

            # 200: API 层成功
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    logger.info("Telegram 消息发送成功")
                    return True

                # 200 但 ok=false（少见，但保留处理）
                error_desc = result.get("description", "未知错误")
                logger.error(f"Telegram 返回错误: {error_desc}")
                return False

            # 429: 限流
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 2**attempt))
                if attempt < max_retries:
                    logger.warning(
                        f"Telegram rate limited, retrying in {retry_after}s "
                        f"(attempt {attempt}/{max_retries})..."
                    )
                    time.sleep(retry_after)
                    continue
                logger.error(f"Telegram rate limited after {max_retries} attempts")
                return False

            # 5xx：服务端错误可重试
            if response.status_code >= 500 and attempt < max_retries:
                delay = 2**attempt
                logger.warning(
                    f"Telegram server error HTTP {response.status_code} "
                    f"(attempt {attempt}/{max_retries}), retrying in {delay}s..."
                )
                time.sleep(delay)
                continue

            # 其他错误：尝试纯文本回退（尤其是 400: can't parse entities）
            logger.error(f"Telegram 请求失败: HTTP {response.status_code}")
            logger.error(f"响应内容: {response.text[:500]}")

            # 纯文本回退：去掉 parse_mode，直接发原始 text（保证不丢消息）
            try:
                logger.info("尝试使用纯文本格式重新发送(回退)...")
                plain_payload = dict(payload)
                plain_payload.pop("parse_mode", None)
                plain_payload["text"] = text
                response_plain = requests.post(api_url, json=plain_payload, timeout=10)
                if response_plain.status_code == 200 and response_plain.json().get(
                    "ok"
                ):
                    logger.info("Telegram 消息发送成功（纯文本回退）")
                    return True
            except Exception as e:
                logger.error(f"Telegram plain-text fallback failed: {e}")

            return False

        return False

    def _send_telegram_chunked(
        self,
        api_url: str,
        chat_id: str,
        content: str,
        max_length: int,
        message_thread_id: Optional[str] = None,
    ) -> bool:
        """分段发送长 Telegram 消息"""
        # 按段落分割
        sections = content.split("\n---\n")

        current_chunk = []
        current_length = 0
        all_success = True
        chunk_index = 1

        for section in sections:
            section_length = len(section) + 5  # +5 for "\n---\n"

            if current_length + section_length > max_length:
                # 发送当前块
                if current_chunk:
                    chunk_content = "\n---\n".join(current_chunk)
                    logger.info(f"发送 Telegram 消息块 {chunk_index}...")
                    if not self._send_telegram_message(
                        api_url, chat_id, chunk_content, message_thread_id
                    ):
                        all_success = False
                    chunk_index += 1

                # 重置
                current_chunk = [section]
                current_length = section_length
            else:
                current_chunk.append(section)
                current_length += section_length

        # 发送最后一块
        if current_chunk:
            chunk_content = "\n---\n".join(current_chunk)
            logger.info(f"发送 Telegram 消息块 {chunk_index}...")
            if not self._send_telegram_message(
                api_url, chat_id, chunk_content, message_thread_id
            ):
                all_success = False

        return all_success

    def _send_telegram_photo(self, image_bytes: bytes) -> bool:
        """Send image via Telegram sendPhoto API (Issue #289)."""
        if not self._is_telegram_configured():
            return False
        bot_token = self._telegram_config["bot_token"]
        chat_id = self._telegram_config["chat_id"]
        message_thread_id = self._telegram_config.get("message_thread_id")
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            data = {"chat_id": chat_id}
            if message_thread_id:
                data["message_thread_id"] = message_thread_id
            files = {"photo": ("report.png", image_bytes, "image/png")}
            response = requests.post(api_url, data=data, files=files, timeout=30)
            if response.status_code == 200 and response.json().get("ok"):
                logger.info("Telegram 图片发送成功")
                return True
            logger.error("Telegram 图片发送失败: %s", response.text[:200])
            return False
        except Exception as e:
            logger.error("Telegram 图片发送异常: %s", e)
            return False

    def _convert_to_telegram_markdown(self, text: str) -> str:
        """
        将标准 Markdown 转换为 Telegram 支持的格式

        Telegram Markdown (旧版) 限制：
        - 标题可以用 *加粗* 替代
        - 使用 *bold* 而非 **bold**
        - 使用 _italic_
        - 不需要转义括号[]()
        """
        result = text

        # 将 # 标题转换为 *加粗* (Telegram不支持#，转成加粗更好看)
        # 例如：# 标题 -> *标题*
        result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

        # 转换 **bold** 为 *bold*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # 移除之前错误的多余括号转义，因为旧版 Markdown 不需要转义 [ ] ( )
        # 如果你文本里有没闭合的 * 或 _，可能会触发 400 错误，然后触发上面的纯文本降级

        return result

    def _convert_to_telegram_html(self, text: str) -> str:
        """
        Convert a Markdown-ish report to Telegram HTML.
        Goals:
        - stable (avoid 'can't parse entities')
        - readable on mobile
        - handle headings/bold/blockquote/links/code and markdown tables gracefully
        """
        import re

        raw = text or ""

        # -------- helpers --------
        def html_escape(s: str) -> str:
            # escape first to avoid breaking HTML parse
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def convert_markdown_links(s: str) -> str:
            # text [<sup>1</sup>](url) -> <a href="url">text</a>
            # url 中有 ) 的情况这里不完美，但对常规链接够用
            return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)

        def convert_inline_code_and_bold(s: str) -> str:
            # 先处理行内代码，避免里面的 ** 被误判
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            # **bold** -> <b>bold</b>
            s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
            return s

        def convert_headings(line: str) -> str:
            # # 标题 -> <b>标题</b>
            m = re.match(r"^(#{1,6})\s+(.+)$", line)
            if not m:
                return line
            title = m.group(2).strip()
            return f"<b>{title}</b>"

        def convert_hr(line: str) -> str:
            # --- -> 分隔线（Telegram HTML 没有 <hr>，用等宽横线更稳定）
            if re.match(r"^\s*---\s*$", line):
                return "<pre>────────────────────────</pre>"
            return line

        def is_table_line(line: str) -> bool:
            # 粗略判断 markdown table 行：包含 | 并且两端也常是 |
            return (
                ("|" in line)
                and (line.strip().startswith("|"))
                and (line.strip().endswith("|"))
            )

        def is_table_sep(line: str) -> bool:
            # |---|---| 这种分隔线
            t = line.strip()
            if not is_table_line(t):
                return False
            inner = t.strip("|").strip()
            # 允许 :---: 对齐格式
            return all(
                set(cell.strip()) <= set("-:") and cell.strip()
                for cell in inner.split("|")
            )

        def table_to_pre(table_lines: list[str]) -> str:
            # 把 markdown table 转成等宽文本
            rows = []
            for ln in table_lines:
                ln = ln.strip()
                if is_table_sep(ln):
                    continue
                cells = [c.strip() for c in ln.strip("|").split("|")]
                rows.append(cells)

            if not rows:
                return ""

            # 列宽（按字符长度）——中文会略有偏差，但比原生表格好读
            col_count = max(len(r) for r in rows)
            for r in rows:
                while len(r) < col_count:
                    r.append("")

            widths = [0] * col_count
            for r in rows:
                for i, c in enumerate(r):
                    widths[i] = max(widths[i], len(c))

            def fmt_row(r: list[str]) -> str:
                parts = []
                for i, c in enumerate(r):
                    pad = widths[i] - len(c)
                    parts.append(c + (" " * pad))
                return " | ".join(parts)

            out_lines = []
            out_lines.append(fmt_row(rows[0]))
            out_lines.append("-" * min(120, max(10, len(out_lines[0]))))
            for r in rows[1:]:
                out_lines.append(fmt_row(r))

            return "<pre>" + "\n".join(out_lines) + "</pre>"

        # -------- main conversion pipeline --------

        # 0) 先整体 escape，保证 HTML 不会炸
        escaped = html_escape(raw)

        # 1) 先把 ```code block``` 转成 <pre>...</pre>
        # 注意：这里 escaped 后的内容没有 < >，所以 pre 内安全
        def repl_fenced(m):
            code = m.group(1).strip("\n")
            return "<pre>" + code + "</pre>"

        escaped = re.sub(r"```(.*?)```", repl_fenced, escaped, flags=re.DOTALL)

        # 2) 按行处理（标题/引用/分隔线/表格）
        lines = escaped.splitlines()
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]

            # 2.1) 收集 markdown 表格块
            if is_table_line(line):
                tbl = [line]
                j = i + 1
                while j < len(lines) and is_table_line(lines[j]):
                    tbl.append(lines[j])
                    j += 1
                out.append(table_to_pre(tbl))
                i = j
                continue

            # 2.2) 标题
            line2 = convert_headings(line)

            # 2.3) 引用：以 &gt; 开头（因为我们 escape 过）
            if line2.lstrip().startswith("&gt;"):
                quote = line2.lstrip()[4:].strip()  # remove "&gt;"
                line2 = f"<blockquote>{quote}</blockquote>"

            # 2.4) 分隔线
            line2 = convert_hr(line2)

            out.append(line2)
            i += 1

        html_text = "\n".join(out)

        # 3) 链接（此时文本已经 escape 过，不会把 url 里的 & 破坏？会被变成 &amp;，通常也能用）
        html_text = convert_markdown_links(html_text)

        # 4) 行内代码 + 加粗
        html_text = convert_inline_code_and_bold(html_text)

        # 5) 一点点清理：连续多空行不动，Telegram 能处理
        return html_text
