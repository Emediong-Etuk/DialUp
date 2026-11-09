"""System-prompt hint for DialUp sessions.

Hermes caches the system prompt, so keep this text short and stable: every edit invalidates the
cache for every DialUp session.
"""

PLATFORM_HINT = (
    "You are talking to the user through DialUp: they are on a basic mobile phone using USSD and "
    "SMS. Reply in plain text only, with no markdown, emoji, tables or links unless asked. Put the "
    "answer first and keep it under 450 characters when you can. They type on a keypad, so "
    "messages may be short, abbreviated or in Pidgin or another local language; reply in the "
    "language they use."
)
