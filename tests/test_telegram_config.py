import pytest

from telegram_client import _optional_chat_id


def test_parse_private_channel_chat_id():
    assert _optional_chat_id("-1001234567890") == -1001234567890


def test_reject_invite_link_as_chat_id():
    with pytest.raises(RuntimeError, match="邀请链接"):
        _optional_chat_id("https://t.me/+example")
