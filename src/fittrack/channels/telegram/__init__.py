"""The Telegram adapter (spec 18.2), phase 1.0's only channel.

Nothing outside `fittrack.channels` imports this package: the registry builds it
lazily and hands out a `Channel` (spec 18.1), and
`tests/unit/test_channel_contract.py` checks that across `src/`.

The four modules split along the lines that leak:

- `secret` compares the webhook's shared secret, in constant time.
- `client` is the only code that holds the bot token, and the only code that
  builds a URL containing it.
- `markup` translates the neutral dialect the agent writes into Telegram HTML,
  escaping everything that is not markup first (spec 13.4).
- `adapter` is the `Channel`, and knows nothing about HTTP beyond the client.
"""

from fittrack.channels.telegram.adapter import TelegramAdapter

__all__ = ["TelegramAdapter"]
