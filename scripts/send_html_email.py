#!/usr/bin/env python3
"""
将 Markdown 文本/文件转换为排版美观的 HTML 邮件并通过 agently-cli 发送。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


EMAIL_CSS = """
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #24292e;
    line-height: 1.6;
    padding: 16px;
    background-color: #ffffff;
  }
  h1, h2, h3, h4, h5, h6 {
    margin-top: 24px;
    margin-bottom: 12px;
    font-weight: 600;
    line-height: 1.25;
    color: #1a202c;
  }
  h1 { font-size: 1.6em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }
  h2 { font-size: 1.3em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }
  h3 { font-size: 1.1em; }
  p, ul, ol { margin-top: 0; margin-bottom: 14px; }
  ul, ol { padding-left: 24px; }
  li { margin-bottom: 4px; }
  code {
    padding: 0.2em 0.4em;
    margin: 0;
    font-size: 85%;
    background-color: rgba(175, 184, 193, 0.2);
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  pre {
    padding: 12px 16px;
    overflow: auto;
    font-size: 85%;
    line-height: 1.45;
    background-color: #f6f8fa;
    border-radius: 6px;
    border: 1px solid #e1e4e8;
  }
  pre code {
    background-color: transparent;
    padding: 0;
  }
  blockquote {
    padding: 0 1em;
    color: #57606a;
    border-left: 0.25em solid #d0d7de;
    margin: 0 0 14px 0;
  }
  table {
    border-spacing: 0;
    border-collapse: collapse;
    margin-bottom: 16px;
    width: 100%;
    overflow: auto;
  }
  table th, table td {
    padding: 6px 13px;
    border: 1px solid #d0d7de;
  }
  table tr:nth-child(2n) {
    background-color: #f6f8fa;
  }
  table th {
    font-weight: 600;
    background-color: #f0f2f5;
  }
  hr {
    height: 0.25em;
    padding: 0;
    margin: 24px 0;
    background-color: #d0d7de;
    border: 0;
  }
  a {
    color: #0969da;
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
</style>
"""


def markdown_to_html(md_text: str) -> str:
    """使用 marked 将 Markdown 渲染为 HTML"""
    # 尝试 marked CLI
    try:
        res = subprocess.run(
            ["marked", "--gfm", "--breaks"],
            input=md_text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            return f"{EMAIL_CSS}\n<div class='markdown-body'>\n{res.stdout}\n</div>"
    except Exception:
        pass

    # 备选 Python markdown 库
    try:
        import markdown
        html = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
        return f"{EMAIL_CSS}\n<div class='markdown-body'>\n{html}\n</div>"
    except Exception:
        pass

    # 最底限保底
    return f"<pre style='font-family: monospace; white-space: pre-wrap;'>{md_text}</pre>"


def send_mail(to: str, subject: str, md_content: str) -> bool:
    html_body = markdown_to_html(md_content)
    cmd = [
        "agently-cli", "message", "+send",
        "--to", to,
        "--subject", subject,
        "--body", html_body,
        "--confirmed"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode == 0:
        print(f"✅ 邮件已成功发送至 {to}（主题: {subject}）")
        return True
    else:
        print(f"❌ 邮件发送失败: {res.stderr or res.stdout}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="将 Markdown 转换为 HTML 并发送邮件")
    parser.add_argument("--to", default="709934831@qq.com", help="收件人邮箱")
    parser.add_argument("--subject", required=True, help="邮件主题")
    parser.add_argument("--file", help="Markdown 文件路径")
    parser.add_argument("--text", help="Markdown 文本内容")
    args = parser.parse_args()

    md_text = ""
    if args.file:
        p = Path(args.file)
        if p.exists():
            md_text = p.read_text(encoding="utf-8")
        else:
            print(f"文件不存在: {args.file}", file=sys.stderr)
            sys.exit(1)
    elif args.text:
        md_text = args.text
    else:
        # 从 stdin 读取
        md_text = sys.stdin.read()

    if not md_text.strip():
        print("未提供有效的 Markdown 内容", file=sys.stderr)
        sys.exit(1)

    success = send_mail(args.to, args.subject, md_text)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
