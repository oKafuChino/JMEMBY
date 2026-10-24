"""Command parsing and chat boundaries, independent of Telegram transport."""


def parse_command(text):
    parts = (text or "").strip().split(maxsplit=1)
    head = parts[0] if parts else ""
    command, _, recipient = head.partition("@")
    return command, recipient, parts[1] if len(parts) > 1 else ""


def chat_allows(chat_type, chat_id, user_id, command="", action=None):
    if chat_type == "private":
        return chat_id == user_id and command not in {"/kk", "/white"}
    if chat_type in {"group", "supergroup"}:
        return action in {"confirm", "dismiss"} if action is not None else command in {"/jm", "/kk", "/white"}
    return False
