from __future__ import annotations

import re


def format_task_progress(progresses: list) -> str:
    if not progresses:
        return "无任务数据"

    duration_re = re.compile(r"^(.+?)\d+分钟$")
    groups: dict[str, list] = {}
    for task in progresses:
        match = duration_re.match(task.task_name)
        prefix = match.group(1) if match else task.task_name
        groups.setdefault(prefix, []).append(task)
    for tasks in groups.values():
        tasks.sort(key=lambda t: float(t.limit_value))

    bar_width = 20
    lines: list[str] = []
    for prefix, tasks in groups.items():
        if len(tasks) > 1:
            cur = int(max(float(t.cur_value) for t in tasks))
            lines.append(f"{prefix} (当前: {cur} 分钟)")
            for task in tasks:
                target = int(float(task.limit_value))
                pct = min(
                    100,
                    int(float(task.cur_value) / max(1, float(task.limit_value)) * 100),
                )
                filled = int(bar_width * pct / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                if task.is_completed:
                    status = " ✔ 完成"
                else:
                    status = f" {pct:>3}%"
                lines.append(f"  {bar} {target:>4}分{status}")
        else:
            task = tasks[0]
            target = int(float(task.limit_value))
            cur = int(float(task.cur_value))
            pct = min(100, int(cur / max(1, target) * 100))
            filled = int(bar_width * pct / 100)
            bar = "█" * filled + "░" * (bar_width - filled)
            if task.is_completed:
                status = " ✔ 完成"
            else:
                status = f" {pct:>3}%"
            lines.append(f"{task.task_name} ({cur}/{target})")
            lines.append(f"  {bar}{status}")
    return "\n".join(lines)


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

