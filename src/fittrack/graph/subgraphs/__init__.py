"""Domain subgraphs: ingestion, analysis, recommendation, admin (spec 8.5, 8.6).

None of these may know which channel a message arrived on. The difference
between Telegram and WhatsApp is format, decided at the end — never content,
decided in the middle (AD-39).
"""
