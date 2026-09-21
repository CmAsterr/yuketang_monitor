"""Shared answer presentation. No filesystem, network, or application state."""

from __future__ import annotations
import copy
import html
from markdown_it import MarkdownIt
from .ai import parse_answer


def normalize_answer(answer):
    result = copy.deepcopy(answer) if isinstance(answer, dict) else {}
    values = result.get("answer")
    if isinstance(values, str):
        values = [values]
    if (
        isinstance(values, list)
        and len(values) == 1
        and isinstance(values[0], str)
        and '"answer"' in values[0]
        and "{" in values[0]
    ):
        parsed = parse_answer(values[0])
        if parsed.get("structured"):
            result.update(parsed)
    if result.get("answer") == [] and result.get("reasoning"):
        result["unanswerable"] = True
    return result


def _math_html(latex, display=False):
    from latex2mathml.converter import convert

    try:
        value = convert(latex.strip())
    except Exception:
        return f'<code class="math-fallback">{html.escape(latex.strip())}</code>'
    if display:
        return f'<div class="math-block">{value}</div>'
    return f'<span class="math-inline">{value}</span>'


def markdown(text):
    """Render Markdown plus the project's $ / $$ math convention safely.

    Math is converted to browser-native MathML so the webpage and QQ Markdown
    share the same dollar-delimiter source without loading a remote renderer.
    """
    import re

    raw = str(text or "").replace("\\\\$", "$")
    placeholders = {}

    def put(value, display):
        key = f"@@YKT_MATH_{len(placeholders)}@@"
        placeholders[key] = _math_html(value, display)
        return key

    # Normalize legacy delimiters at the presentation boundary only.
    raw = re.sub(
        r"\\\\\[(.*?)\\\\\]", lambda m: "$$\n" + m.group(1) + "\n$$", raw, flags=re.S
    )
    raw = re.sub(
        r"\\\\\((.*?)\\\\\)", lambda m: "$" + m.group(1) + "$", raw, flags=re.S
    )
    raw = re.sub(r"\$\$(.*?)\$\$", lambda m: put(m.group(1), True), raw, flags=re.S)
    raw = re.sub(
        r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)",
        lambda m: put(m.group(1), False),
        raw,
        flags=re.S,
    )

    renderer = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
    renderer.disable("image")
    output = renderer.render(raw)
    for key, value in placeholders.items():
        output = output.replace(f"<p>{key}</p>", value).replace(key, value)
    return output


def confidence_text(answer):
    value = answer.get("confidence")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        return "未提供"
    return f"{value:.0%}"


def confidence_label(answer):
    value = answer.get("confidence")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        return "未提供（模型未返回自评）"
    return f"{value:.0%}（模型自评）"


def present(row):
    result = copy.deepcopy(row)
    a = normalize_answer(row.get("answer"))
    result["answer"] = a
    values = a.get("answer") or []
    if isinstance(values, str):
        values = [values]
    result["display"] = {
        "answer_html": markdown(
            "无法作答" if a.get("unanswerable") else "\n\n".join(str(v) for v in values)
        ),
        "reasoning_html": markdown(a.get("reasoning", "")),
        "confidence": confidence_text(a),
        "confidence_label": confidence_label(a),
    }
    return result


def notification_text(row, phase="result"):
    p = row["payload"]
    a = normalize_answer(row.get("answer"))
    title = "新习题提醒" if phase == "reminder" else "习题解答"
    lines = [f"{title} · {p.get('course', '课堂')}", f"题目编号：{row['problem']}"]
    if p.get("body"):
        lines.append(p["body"])
    if phase == "reminder":
        lines.append("已收到题目，答案生成后继续推送。")
    else:
        values = a.get("answer") or []
        if values or a.get("unanswerable"):
            lines += [
                "答案：" + ("无法作答" if a.get("unanswerable") else "；".join(values)),
                "解析：" + (a.get("reasoning") or "未提供"),
                "可信度："
                + (
                    confidence_label(a).replace(
                        "（模型自评）", "（对无法作答判断的自评）"
                    )
                    if a.get("unanswerable") and a.get("confidence") is not None
                    else confidence_label(a)
                ),
            ]
        else:
            lines.append("处理状态：" + (row.get("error") or "等待解答"))
    if p.get("cover"):
        lines.append("题面图：" + p["cover"])
    return "\n\n".join(lines)
