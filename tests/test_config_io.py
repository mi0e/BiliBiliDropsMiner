"""save_config_data 的原子写入与权限保持。

配置里含明文 Cookie 与通知 URL，所以原子替换不仅要防截断，还必须保留原有的
权限保护：os.replace 会把临时文件的 mode 带到目标路径，按 umask 创建临时文件
会把 0600 放宽成 0644。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bilibili_drops_miner.gui_parts.config_io import save_config_data

POSIX_ONLY = unittest.skipIf(os.name != "posix", "POSIX 权限语义在 Windows 上不适用")


class SaveConfigDataTests(unittest.TestCase):
    @staticmethod
    def _payload() -> dict[str, object]:
        return {"cookie": "SESSDATA=secret; bili_jct=csrf", "room_ids": [1]}

    @POSIX_ONLY
    def test_new_config_file_is_created_with_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"

            save_config_data(path, self._payload())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @POSIX_ONLY
    def test_existing_non_restrictive_permissions_survive_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)

            save_config_data(path, self._payload())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    @POSIX_ONLY
    def test_existing_restrictive_permissions_survive_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o600)

            save_config_data(path, self._payload())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_save_replaces_content_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"

            save_config_data(path, self._payload())
            save_config_data(path, {**self._payload(), "room_ids": [1, 2]})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["room_ids"], [1, 2]
            )
            leftovers = [
                item.name for item in Path(directory).iterdir() if ".tmp" in item.name
            ]
            self.assertEqual(leftovers, [])

    @POSIX_ONLY
    def test_failed_permission_restore_preserves_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"original": true}', encoding="utf-8")
            os.chmod(path, 0o600)

            with patch(
                "bilibili_drops_miner.gui_parts.config_io.os.chmod",
                side_effect=PermissionError("chmod denied"),
            ):
                with self.assertRaises(PermissionError):
                    save_config_data(path, self._payload())

            self.assertEqual(path.read_text(encoding="utf-8"), '{"original": true}')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    @POSIX_ONLY
    def test_preexisting_temp_file_is_not_reused_or_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            # 旧命名方案使用 PID 固定后缀，进程崩溃留下的文件会被下一次保存
            # 直接用 open(..., "w") 截断，替换前有一段时间凭据是 0644。
            old_temp = path.with_name(f"{path.name}.tmp{os.getpid()}")
            old_temp.write_text("stale", encoding="utf-8")
            os.chmod(old_temp, 0o644)

            save_config_data(path, self._payload())

            self.assertEqual(old_temp.read_text(encoding="utf-8"), "stale")
            leftovers = list(Path(directory).glob(f"{path.name}.tmp{os.getpid()}-*"))
            self.assertEqual(leftovers, [])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), self._payload()
            )

    def test_failed_replace_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"

            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    save_config_data(path, self._payload())

            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
