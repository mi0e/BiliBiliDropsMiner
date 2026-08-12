from __future__ import annotations

import re
import unicodedata

from bilibili_drops_miner.client import LiveWatchTime


def format_seconds_zh(total_seconds: int | float) -> str:
    seconds = max(0, int(total_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}小时{minutes:02d}分{seconds:02d}秒"
    if minutes > 0:
        return f"{minutes}分{seconds:02d}秒"
    return f"{seconds}秒"


def format_live_watch_time_progress(
    watch_times: list[LiveWatchTime],
    baselines: dict[int, int],
) -> str:
    if not watch_times:
        return "本次预估观看时长: 无数据"

    parts: list[tuple[str, str]] = []
    for item in watch_times:
        baseline = baselines.get(item.room_id, item.watch_time)
        elapsed = max(0, item.watch_time - baseline)
        room_label = f"房间 {item.room_id}"
        if item.rusername:
            room_label = f"{room_label}，{item.rusername}"
        parts.append((room_label, format_seconds_zh(elapsed)))

    if len(parts) == 1:
        room_label, duration = parts[0]
        return f"本次预估观看时长: {duration} ({room_label})"
    return "本次预估观看时长: " + " | ".join(
        f"{room_label}: {duration}" for room_label, duration in parts
    )


BAR_WIDTH = 20
DURATION_TASK_RE = re.compile(r"^(.+?)\d+分钟$")


def _display_width(text: str) -> int:
    width = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        # Windows/Qt 下的等宽字体通常把弯引号等 Ambiguous 字符按窄字符渲染。
        if unicodedata.east_asian_width(char) in {"F", "W"}:
            width += 2
        else:
            width += 1
    return width


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _format_task_number(value: int | float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _progress_percent(cur_value: int | float, limit_value: int | float) -> int:
    try:
        cur = float(cur_value)
        limit = float(limit_value)
    except (TypeError, ValueError):
        return 0
    if limit <= 0:
        return 0
    return max(0, min(100, int(cur / limit * 100)))


def _progress_bar(percent: int) -> str:
    filled = int(BAR_WIDTH * percent / 100)
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def _status_text(item, percent: int) -> str:
    if getattr(item, "status", None) == 6:
        return "✔ 已领取"
    if getattr(item, "is_completed", False):
        return "✔ 完成"
    return f"{percent:>3}%"


def _short_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[-4:]


def _label_with_id(name: str, item_id: str) -> str:
    label = str(name or item_id or "未命名任务").strip()
    short_id = _short_id(item_id)
    if short_id:
        return f"{label} [{short_id}]"
    return label


def _progress_value(item) -> str:
    cur = _format_task_number(getattr(item, "cur_value", 0))
    limit = _format_task_number(getattr(item, "limit_value", 0))
    return f"{cur}/{limit}"


def _format_progress_inline(item, value_width: int = 0) -> str:
    value = _progress_value(item)
    if value_width > 0:
        value = value.rjust(value_width)
    percent = _progress_percent(
        getattr(item, "cur_value", 0),
        getattr(item, "limit_value", 0),
    )
    return f"{value}  {_progress_bar(percent)} {_status_text(item, percent)}"


def _same_number(left: int | float, right: int | float) -> bool:
    try:
        return abs(float(left) - float(right)) < 0.000001
    except (TypeError, ValueError):
        return False


def _has_meaningful_checkpoints(task) -> bool:
    check_points = getattr(task, "check_points", []) or []
    if len(check_points) > 1:
        return True
    if not check_points:
        return False

    point = check_points[0]
    alias = str(getattr(point, "alias", "") or "").strip()
    task_name = str(getattr(task, "task_name", "") or "").strip()
    return (
        (alias and alias != task_name)
        or not _same_number(getattr(point, "cur_value", 0), task.cur_value)
        or not _same_number(getattr(point, "limit_value", 0), task.limit_value)
    )


def _format_award_info(point) -> str:
    award_name = str(getattr(point, "award_name", "") or "").strip()
    award_count = getattr(point, "award_count", 0)
    try:
        has_count = float(award_count) > 0
    except (TypeError, ValueError):
        has_count = False
    if award_name and has_count:
        return f"{award_name} x{_format_task_number(award_count)}"
    return award_name


def _progress_row(
    prefix: str,
    item,
    suffix: str = "",
) -> tuple[str, str, object | None, str]:
    return "progress", prefix, item, suffix


def _text_row(text: str) -> tuple[str, str, object | None, str]:
    return "text", text, None, ""


def _render_rows(rows: list[tuple[str, str, object | None, str]]) -> list[str]:
    progress_rows = [row for row in rows if row[0] == "progress"]
    prefix_width = max((_display_width(row[1]) for row in progress_rows), default=0)
    value_width = max((len(_progress_value(row[2])) for row in progress_rows), default=0)

    lines: list[str] = []
    for kind, prefix, item, suffix in rows:
        if kind != "progress" or item is None:
            lines.append(prefix)
            continue
        lines.append(
            f"{_pad_display(prefix, prefix_width)}  "
            f"{_format_progress_inline(item, value_width)}{suffix}"
        )
    return lines


def _format_checkpoint_row(point) -> tuple[str, str, object | None, str]:
    label = _label_with_id(getattr(point, "alias", ""), getattr(point, "sid", ""))
    award_info = _format_award_info(point)
    suffix = f"  {award_info}" if award_info else ""
    return _progress_row(f"   - {label}", point, suffix)


def _format_task_row(index: int, task) -> tuple[str, str, object | None, str]:
    label = _label_with_id(getattr(task, "task_name", ""), getattr(task, "task_id", ""))
    return _progress_row(f"{index}. {label}", task)


def _format_structured_task(index: int, task) -> list[tuple[str, str, object | None, str]]:
    lines = [_format_task_row(index, task)]
    for point in getattr(task, "check_points", []) or []:
        lines.append(_format_checkpoint_row(point))
    return lines


def _format_duration_group(
    index: int,
    prefix: str,
    tasks: list,
) -> list[tuple[str, str, object | None, str]]:
    sorted_tasks = sorted(tasks, key=lambda item: float(item.limit_value))
    cur = max(float(task.cur_value) for task in sorted_tasks)
    lines = [_text_row(f"{index}. {prefix} (当前: {_format_task_number(cur)} 分钟)")]
    for task in sorted_tasks:
        target = _format_task_number(task.limit_value)
        lines.append(_progress_row(f"   - {_label_with_id(target + '分', task.task_id)}", task))
    return lines


def _format_flat_tasks(flat_tasks: list[tuple[int, object]]) -> list[tuple[str, str, object | None, str]]:
    groups: dict[str, list[object]] = {}
    sequence: list[tuple[str, int, object | str]] = []

    for index, task in flat_tasks:
        match = DURATION_TASK_RE.match(str(getattr(task, "task_name", "")))
        if match:
            prefix = match.group(1)
            if prefix not in groups:
                sequence.append(("group", index, prefix))
            groups.setdefault(prefix, []).append(task)
            continue
        sequence.append(("task", index, task))

    lines: list[tuple[str, str, object | None, str]] = []
    for kind, index, payload in sequence:
        if kind == "group":
            group_tasks = groups[str(payload)]
            if len(group_tasks) > 1:
                lines.extend(_format_duration_group(index, str(payload), group_tasks))
            else:
                lines.append(_format_task_row(index, group_tasks[0]))
            continue
        lines.append(_format_task_row(index, payload))
    return lines


def format_task_progress(progresses: list) -> str:
    if not progresses:
        return "无任务数据"

    rows: list[tuple[str, str, object | None, str]] = []
    flat_tasks: list[tuple[int, object]] = []
    for index, task in enumerate(progresses, start=1):
        if _has_meaningful_checkpoints(task):
            if flat_tasks:
                rows.extend(_format_flat_tasks(flat_tasks))
                flat_tasks = []
            rows.extend(_format_structured_task(index, task))
            continue
        flat_tasks.append((index, task))

    if flat_tasks:
        rows.extend(_format_flat_tasks(flat_tasks))
    return "\n".join(_render_rows(rows))


def format_reward_claim_results(results: list) -> str:
    if not results:
        return "无可领取任务数据"

    success_count = sum(1 for item in results if item.success and not item.skipped)
    claimed_count = sum(1 for item in results if item.success and item.skipped)
    skipped_count = sum(1 for item in results if item.skipped and not item.success)
    failed_count = sum(1 for item in results if not item.success and not item.skipped)

    lines = [
        "领取结果",
        (
            f"领取成功 {success_count} 个，已领取 {claimed_count} 个，"
            f"跳过 {skipped_count} 个，失败 {failed_count} 个"
        ),
        "",
    ]
    for item in results:
        if item.success and item.skipped:
            prefix = "已领取"
        elif item.success:
            prefix = "领取成功"
        elif item.skipped:
            prefix = "跳过"
        else:
            prefix = "失败"

        name = item.task_name or item.task_id
        reward = f" - {item.reward_name}" if item.reward_name else ""
        if item.success:
            lines.append(f"[{prefix}] {name}{reward}")
            continue

        message = clean_reward_message(item.message, item.skipped)
        if item.code is not None and item.code != 0:
            message = f"{message} (code={item.code})"
        lines.append(f"[{prefix}] {name}{reward}: {message}")
    return "\n".join(lines)


def clean_reward_message(message: str, skipped: bool) -> str:
    cleaned = str(message or "").strip()
    if cleaned in {"", "0", "查看奖励"}:
        if skipped:
            return "当前不可领取"
        return "领取失败"
    return cleaned
