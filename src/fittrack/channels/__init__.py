"""The only package that knows a messaging protocol (AD-39, spec 18.1).

`base` holds the contract — the `Channel` protocol, `ChannelCaps`, the types
that cross the boundary and the `ErrorClass` taxonomy — and `registry` decides
which adapters this process builds, from `FITTRACK_CHANNELS`. The concrete
adapters live in subpackages beside them: `telegram/` in phase 1.0, `whatsapp/`
in phase 2.0.

The rule: nothing under `graph/` or `agents/` may import from here, or read
`channel_caps`. The two exceptions are `graph/nodes/voice.py` and
`graph/nodes/deliver.py` (spec 13.5, 18.1). Everywhere else in `src/` may hold a
`Channel`, and never a concrete adapter — the registry is what hands out the
protocol, and `tests/unit/test_channel_contract.py` checks both halves.
"""
