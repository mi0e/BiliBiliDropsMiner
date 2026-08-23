from __future__ import annotations

import json
import unittest

from bilibili_drops_miner.utils import extract_bili_live_task_groups


def _task_group(task_id: str) -> dict:
    return {"tasklist": [{"taskId": task_id}]}


def _panel(panel_id: str, label: str) -> dict:
    return {
        "id": panel_id,
        "tabItem": {"tabItemProps": {"textContent": {"content": label}}},
    }


def _component(name: str, props: dict, children: list[dict] | None = None) -> dict:
    return {
        "type": "Component",
        "name": name,
        "props": props,
        "slots": [{"children": children or []}],
    }


class ExtractBiliLiveTaskGroupsTests(unittest.TestCase):
    def test_extracts_new_eva_layer_tree_and_preserves_groups(self) -> None:
        yesterday_panel = _component(
            "EvaTabs.Panel",
            _panel("yesterday", "昨天"),
            [_component("EraTasklistPc", _task_group("old-task"))],
        )
        today_panel = _component(
            "EvaTabs.Panel",
            _panel("today", "今天"),
            [
                _component("EraTasklistPc", _task_group("today-a")),
                _component("EraTasklistPc", _task_group("today-b")),
            ],
        )
        tabs = _component(
            "EvaTabs",
            {"activatedTabPanelId": "today"},
            [yesterday_panel, today_panel],
        )
        page_data = {"layerTree": _component("Root", {}, [tabs])}
        html = (
            '<script>window.__initialState = {"BaseInfo": {}};</script>'
            "<script>window.__BILIACT_EVAPAGEDATA__ = "
            f"{json.dumps(page_data)};</script>"
        )

        self.assertEqual(
            extract_bili_live_task_groups(html),
            [
                {"label": "昨天", "task_ids": ["old-task"], "active": False},
                {
                    "label": "今天",
                    "task_ids": ["today-a", "today-b"],
                    "active": True,
                },
            ],
        )

    def test_extracts_legacy_initial_state(self) -> None:
        state = {
            "EraTasklistPc": [_task_group("legacy-task")],
            "EvaPositionBox": [{"left": 0, "top": 0}],
            "EvaTabs.Panel": [_panel("legacy", "旧活动")],
            "EvaTabs": [{"activatedTabPanelId": "legacy"}],
        }
        html = f"<script>window.__initialState = {json.dumps(state)};</script>"

        self.assertEqual(
            extract_bili_live_task_groups(html),
            [
                {
                    "label": "旧活动",
                    "task_ids": ["legacy-task"],
                    "active": True,
                }
            ],
        )

    def test_returns_empty_for_missing_or_invalid_page_data(self) -> None:
        invalid_pages = [
            "",
            "<html>no state</html>",
            '<script>window.__BILIACT_EVAPAGEDATA__ = {"layerTree": [};</script>',
            '<script>window.__initialState = {"EraTasklistPc": [};</script>',
        ]

        for html in invalid_pages:
            with self.subTest(html=html):
                self.assertEqual(extract_bili_live_task_groups(html), [])


if __name__ == "__main__":
    unittest.main()
