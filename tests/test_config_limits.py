"""配置边界测试。

此前「每房间线程数 ≤ 128」这个边界只存在于 WebUI 侧（Settings 与 index.html
的 max），MinerConfig.validate() 只检查「大于 0」，GUI 与 CLI 完全没有上限：
输入 99999 会一路走到 miner 的 range(1, thread_count + 1) 去开线程。
这里把边界固定住，并确认 WebUI 与 MinerConfig 用的是同一个值。
"""

from __future__ import annotations

import unittest

from bilibili_drops_miner.config import MAX_ROOM_COUNT, MAX_THREAD_COUNT, MinerConfig


def config(**overrides) -> MinerConfig:
    kwargs = {"cookie": "cookie", "room_ids": [1], "thread_count": 1}
    kwargs.update(overrides)
    return MinerConfig(**kwargs)


class ThreadCountLimitTest(unittest.TestCase):
    def test_limit_matches_webui_boundary(self) -> None:
        # 128 来自 WebUI 已有的 Settings.le，不是新选的值
        self.assertEqual(MAX_THREAD_COUNT, 128)

    def test_upper_bound_accepted(self) -> None:
        config(thread_count=MAX_THREAD_COUNT).validate()

    def test_above_upper_bound_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            config(thread_count=MAX_THREAD_COUNT + 1).validate()
        self.assertIn(str(MAX_THREAD_COUNT), str(ctx.exception))

    def test_extreme_value_rejected(self) -> None:
        # GUI 里裸 QLineEdit 曾经能输入这个值
        with self.assertRaises(ValueError):
            config(thread_count=99999).validate()

    def test_zero_and_negative_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            config(thread_count=0).validate()
        with self.assertRaises(ValueError):
            config(thread_count=-1).validate()

    def test_error_message_is_in_chinese(self) -> None:
        # 错误文案会直接显示给用户，不该出现英文
        with self.assertRaises(ValueError) as ctx:
            config(thread_count=MAX_THREAD_COUNT + 1).validate()
        self.assertRegex(str(ctx.exception), r"[一-鿿]")


class RoomCountLimitTest(unittest.TestCase):
    def test_limit_matches_webui_boundary(self) -> None:
        self.assertEqual(MAX_ROOM_COUNT, 16)

    def test_upper_bound_accepted(self) -> None:
        config(room_ids=list(range(1, MAX_ROOM_COUNT + 1))).validate()

    def test_above_upper_bound_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            config(room_ids=list(range(1, MAX_ROOM_COUNT + 2))).validate()
        self.assertIn(str(MAX_ROOM_COUNT), str(ctx.exception))


class WebUiSettingsShareTheSameLimitsTest(unittest.TestCase):
    def test_settings_use_config_constants(self) -> None:
        from bilibili_drops_miner.web import Settings

        thread_field = Settings.model_fields["thread_count"]
        upper = next(
            (c.le for c in thread_field.metadata if getattr(c, "le", None) is not None),
            None,
        )
        self.assertEqual(upper, MAX_THREAD_COUNT)

        room_field = Settings.model_fields["room_ids"]
        max_len = next(
            (
                c.max_length
                for c in room_field.metadata
                if getattr(c, "max_length", None) is not None
            ),
            None,
        )
        self.assertEqual(max_len, MAX_ROOM_COUNT)


if __name__ == "__main__":
    unittest.main()