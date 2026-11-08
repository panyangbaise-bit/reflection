"""A narrow command grammar grants mutation authority independently of model output."""

import re

from portfolio_backend.errors import require


def require_watchlist_intent(name, args, user_message):
    ticker = re.escape(args["ticker"])
    add = name == "add_to_watchlist"
    action = r"(?:加入|添加到|更新到)" if add else r"(?:移出|移除|从监察表删除)"
    # Entire initiating message must be a command, not quoted text, a news excerpt or negation.
    pattern = rf"(?:请)?(?:将|把)?\s*{ticker}\s*{action}\s*(?:我的)?(?:监察表|观察列表|关注列表)"
    if add:
        pattern += r"(?:[，,]\s*目标价\s*(?P<target>\d{1,12}(?:\.\d{1,12})?))?(?:[，,]\s*优先级\s*(?P<priority>[1-5]))?"
    match = re.fullmatch(pattern + r"[。.!]?", user_message.strip(), flags=re.IGNORECASE)
    if not match:
        english = ("add|update") if add else "remove|delete"
        match = re.fullmatch(
            rf"(?:please\s+)?(?:{english})\s+{ticker}\s+(?:to|from|in)\s+(?:my\s+)?watchlist[.!]?",
            user_message.strip(),
            flags=re.IGNORECASE,
        )
    require(
        match is not None,
        "explicit_watchlist_intent_required",
        "请单独发送明确指令，例如：将 510300 加入监察表，目标价 9，优先级 3；或：将 510300 移出监察表",
        403,
    )
    # Only user-authored fields can affect a write. Model-generated notes/targets cannot.
    if add:
        groups = match.groupdict()
        args["target_price"] = groups.get("target")
        args["priority"] = int(groups.get("priority") or 3)
        args["note"] = ""
