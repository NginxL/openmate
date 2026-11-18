"""Getting the matewechat-native program onto this computer.

The program is built from native/matewechat-native in this repository and published as an asset of the GitHub
release `matewechat-native-v<version>`; its size and SHA-256 are pinned here, so the package decides exactly which
bytes run. It is fetched once in the background into the extension's data directory (a temporary file, checked, then
renamed into place), and used from there offline after that. The setting native_path (a developer's own build) skips
all of this.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys
import tempfile
import threading
import urllib.request
from typing import Optional

log = logging.getLogger("openmate.wechat.install")

VERSION = "0.1.11"
URL = "https://github.com/NginxL/openmate/releases/download/matewechat-native-v{version}/{name}"
ASSETS = {
    ("darwin", "arm64"): {
        "name": "matewechat-native-0.1.11-darwin-arm64",
        "sha256": "4c4a8a992a0a9cda251c29a3cecca3503dde1f617dd224fbb9f411f8444ce36e",
        "size": 8332096,
    },
}
DOWNLOAD_TIMEOUT = 120.0


def host() -> tuple:
    return (sys.platform, platform.machine().lower())


def asset() -> Optional[dict]:
    return ASSETS.get(host())


class Installer:
    """Makes sure the program is there; path() waits for it a little."""

    def __init__(self, data_dir: str, configured: str = ""):
        self.configured = configured.strip()
        self.dir = os.path.join(data_dir, "native", VERSION)
        self.error = ""
        self._done = threading.Event()
        self._started = False
        self._mu = threading.Lock()

    def target(self) -> str:
        a = asset()
        return os.path.join(self.dir, a["name"]) if a else ""

    def start(self) -> None:
        with self._mu:
            if self._started:
                return
            self._started = True
        if self.configured:
            if not os.path.isfile(self.configured):
                self.error = "设置里的微信组件路径不存在：%s" % self.configured
            self._done.set()
            return
        a = asset()
        if a is None:
            self.error = "微信组件只支持 Apple 芯片的 Mac（这台是 %s %s）" % host()
            self._done.set()
            return
        if self._valid(self.target(), a):
            self._done.set()
            return
        threading.Thread(target=self._fetch, args=(a,), name="matewechat-install", daemon=True).start()

    def path(self, wait: float = 8.0) -> str:
        self.start()
        if not self._done.wait(wait):
            raise _not_ready("微信组件还在下载，请稍后再试")
        if self.error:
            raise _not_ready(self.error)
        return self.configured or self.target()

    @staticmethod
    def _valid(path: str, a: dict) -> bool:
        if not os.path.isfile(path) or os.path.getsize(path) != a["size"]:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == a["sha256"]

    def _fetch(self, a: dict) -> None:
        url = URL.format(version=VERSION, name=a["name"])
        tmp = None
        try:
            os.makedirs(self.dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".download-", dir=self.dir)
            h = hashlib.sha256()
            got = 0
            req = urllib.request.Request(url, headers={"User-Agent": "openmate-wechat"})
            # urllib follows GitHub's redirect to its asset host; a proxy from the environment is fine for this one
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > a["size"]:
                        raise ValueError("比预期的大")
                    h.update(chunk)
                    out.write(chunk)
            if got != a["size"] or h.hexdigest() != a["sha256"]:
                raise ValueError("校验不对（大小 %d，期望 %d）" % (got, a["size"]))
            os.chmod(tmp, 0o755)
            os.replace(tmp, self.target())
            tmp = None
            log.info("matewechat-native %s installed", VERSION)
        except Exception as e:  # network, disk, checksum
            self.error = "下载微信组件失败（%s），重启扩展可以再试" % e
            log.warning("matewechat-native download failed: %s", e)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._done.set()


def _not_ready(message: str):
    from openmate_core.backend import BackendError

    return BackendError(message, "config")
