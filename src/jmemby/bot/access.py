"""Command parsing and chat boundaries, independent of Telegram transport."""

GROUP_USER_COMMANDS = frozenset({'/jm', '/like', '/report', '/random', '/rank'})


def parse_command(text):
    parts = (text or "").strip().split(maxsplit=1)
    head = parts[0] if parts else ""
    command, _, recipient = head.partition("@")
    return command, recipient, parts[1] if len(parts) > 1 else ""


def jm_reply_options(message):
    """Reply to the /jm sender's command, even if it replies to somebody else."""
    if parse_command(getattr(message, 'text', '') or '')[0] != '/jm':
        return {}
    return {'reply_parameters': {'message_id': message.message_id, 'allow_sending_without_reply': True}}


def chat_allows(chat_type, chat_id, user_id, command="", action=None):
    if chat_type == "private":
        return chat_id == user_id and command not in {"/kk", "/white"}
    if chat_type in {"group", "supergroup"}:
        return action in {"confirm", "dismiss"} if action is not None else command in GROUP_USER_COMMANDS | {"/kk", "/white"}
    return False
