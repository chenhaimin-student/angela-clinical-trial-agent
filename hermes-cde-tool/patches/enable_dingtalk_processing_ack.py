"""Apply the Angela Demo acknowledgement and delayed-progress patch to Hermes.

The patch is idempotent. It preserves an existing upstream adapter backup and
refuses to edit an unexpected Hermes adapter source layout.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ACK_MARKER = "        # Angela CDE Demo processing acknowledgement\n"
ACK_NEEDLE = "        # Resolve media download codes to URLs so vision tools can use them\n"
ACK_INSERT = """        # Angela CDE Demo processing acknowledgement
        # DingTalk has no typing indicator. Send a small, non-final text reply
        # after the session webhook is available so users know a slow model or
        # CDE lookup is still being processed.
        if os.getenv("DINGTALK_SEND_PROCESSING_ACK", "true").lower() in {"true", "1", "yes", "on"}:
            acknowledgement = await self.send(
                chat_id,
                "\\u5df2\\u6536\\u5230\\uff0c\\u6b63\\u5728\\u5904\\u7406\\u5e76\\u67e5\\u8be2\\u516c\\u5f00\\u8bd5\\u9a8c\\u4fe1\\u606f\\uff0c\\u8bf7\\u7a0d\\u5019\\u2026",
                metadata={"session_webhook": session_webhook},
            )
            if not acknowledgement.success:
                logger.debug("[%s] Processing acknowledgement was not delivered", self.name)

"""

PROGRESS_MARKER = "        # Angela CDE Demo 20-second progress update\n"
HANDLE_NEEDLE = "        await self.handle_message(event)\n"
PROGRESS_INSERT = """        # Angela CDE Demo 20-second progress update
        # The MCP request is synchronous, so this is an honest elapsed-time
        # update rather than an invented candidate count.
        try:
            progress_after_seconds = int(os.getenv("DINGTALK_PROGRESS_AFTER_SECONDS", "20"))
        except ValueError:
            progress_after_seconds = 20
        progress_task = None
        if progress_after_seconds > 0:
            async def _send_delayed_progress() -> None:
                try:
                    await asyncio.sleep(progress_after_seconds)
                    progress = await self.send(
                        chat_id,
                        "\\u5904\\u7406\\u65f6\\u95f4\\u8f83\\u957f\\uff0c\\u6b63\\u5728\\u7ee7\\u7eed\\u6838\\u9a8c\\u516c\\u5f00\\u8bd5\\u9a8c\\u8bb0\\u5f55\\u5e76\\u6574\\u7406\\u56de\\u590d\\u3002",
                        metadata={"session_webhook": session_webhook},
                    )
                    if not progress.success:
                        logger.debug("[%s] Delayed progress update was not delivered", self.name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("[%s] Delayed progress update failed", self.name, exc_info=True)

            progress_task = asyncio.create_task(_send_delayed_progress())
        try:
            await self.handle_message(event)
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
"""



def apply_patch(adapter_path: Path, backup_path: Path) -> str:
    content = adapter_path.read_text(encoding="utf-8")
    original = content
    if ACK_MARKER not in content:
        if content.count(ACK_NEEDLE) != 1:
            raise RuntimeError("Unexpected Hermes adapter version; acknowledgement patch was not applied")
        content = content.replace(ACK_NEEDLE, ACK_INSERT + ACK_NEEDLE)
    if PROGRESS_MARKER not in content:
        if content.count(HANDLE_NEEDLE) != 1:
            raise RuntimeError("Unexpected Hermes adapter version; progress patch was not applied")
        content = content.replace(HANDLE_NEEDLE, PROGRESS_INSERT)
    if content == original:
        return "already-applied"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        shutil.copy2(adapter_path, backup_path)
    adapter_path.write_text(content, encoding="utf-8")
    return "applied"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    print(apply_patch(args.adapter, args.backup))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
