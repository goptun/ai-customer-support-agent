# Postgres as the LangGraph checkpointer; Redis only for short-lived conversation state

A run paused on an Approval Request may wait hours or days for an Attendant, and losing it means losing a money decision. Redis with TTL (the original plan) would silently expire those checkpoints, so checkpoints live in Postgres (`PostgresSaver`), transactional with the Approval Request row. Redis keeps only short-lived Conversation state (history, Intent, Focused Order) with TTL.
