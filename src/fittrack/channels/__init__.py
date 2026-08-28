"""The only package that knows a messaging protocol (AD-39, spec 18.1).

Empty for now: the `Channel` protocol, `ChannelCaps` and the `TelegramAdapter`
land in a later sprint. The package exists ahead of them because the rule that
governs it does — `tests/test_channel_isolation.py` needs somewhere to point,
and a guardrail written after the code it guards has already lost.

The rule: nothing under `graph/` or `agents/` may import from here, or read
`channel_caps`. The two exceptions are `graph/nodes/voice.py` and
`graph/nodes/deliver.py` (spec 13.5, 18.1).
"""
