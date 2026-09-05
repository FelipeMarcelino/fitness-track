"""Provider adapters on the native SDKs (ADR-0011).

Imported by the wiring, never by an agent: the gateway is the only caller, and
no provider object crosses out of this package (spec 7.1, invariant 4).
"""
