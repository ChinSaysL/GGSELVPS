from __future__ import annotations

import manage


def test_menu_actions_point_to_existing_commands() -> None:
    missing = [item.command for item in manage.MENU_ACTIONS if item.command not in manage.COMMANDS]
    assert missing == []


def test_parse_args_defaults_to_menu() -> None:
    args = manage.parse_args([])
    assert args.command == "menu"


def test_parse_args_accepts_direct_command() -> None:
    args = manage.parse_args(["check"])
    assert args.command == "check"
