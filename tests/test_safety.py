from __future__ import annotations

import pytest

from magbot.safety import is_safe


@pytest.mark.parametrize("text", [
    # legacy patterns
    "rm -rf /home",
    "please rm -rf build*",
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=out.img",
    "shutdown now",
    "how do I safely shutdown my pc?",   # legacy: \bshutdown\b blocks the word anywhere
    "px.ok then unlink it",
    # extras
    ":(){ :|:& };:",
    ":(){ :|:&};:",
    "rm -rf ~",
    "rm -rf ~/",
    "rm -rf $HOME",
    "rm -rf /",
    "rm -rf --no-preserve-root /",
    "chmod -R 777 /",
    "echo hi > /dev/sda",
    "cat image.iso > /dev/nvme0n1",
    "cat image.iso | tee /dev/nvme0n1",
    "dd bs=1M of=/dev/sdb",
    "mkfs -t ext4 /dev/sdc",
    "sudo reboot",
    "ls; poweroff",
    "sudo init 0",
    ":> /etc/passwd",
])
def test_dangerous_requests_are_blocked(text):
    assert not is_safe(text)


@pytest.mark.parametrize("text", [
    "write a poem about the sea",
    "how do I reboot my router?",
    "halt the presses, that is news",
    "what does mkfs do, in general terms",
    "rm -rf node_modules",
    "rm -rf ./build",
    "rm -rf ~/projects",
    "rm -rf build/",
    "chmod -R 755 /var/www",
    "chmod -R 777 /var/www/uploads",
    "ls 2>/dev/null",
    "echo hi > notes.txt",
    "search for the best pizza in town",
    "",
])
def test_ordinary_requests_are_allowed(text):
    assert is_safe(text)


def test_matching_is_case_insensitive():
    assert not is_safe("SHUTDOWN")
    assert not is_safe("Sudo Reboot")
