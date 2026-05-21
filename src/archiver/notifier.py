from __future__ import annotations

import shutil
import subprocess

from .config import Settings


def send_notification(settings: Settings, title: str, body: str) -> bool:
    if settings.notify_command:
        subprocess.run(
            [settings.notify_command, title, body],
            check=False,
        )
        return True

    notify_send = shutil.which("notify-send")
    if notify_send is None:
        return False

    subprocess.run([notify_send, title, body], check=False)
    return True
