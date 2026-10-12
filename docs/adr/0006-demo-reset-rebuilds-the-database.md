# The Demo Reset rebuilds the demo database instead of deleting visitor rows

The Audit Log is append-only, enforced by a trigger that rejects UPDATE, DELETE and TRUNCATE. Rather than punching a bypass into that trigger for the daily Demo Reset, or keeping demo Audit Log entries forever, the reset drops the demo schema and rebuilds it (migrations, then seed), together with the LangGraph checkpoints and the Redis working state. The append-only guarantee holds for the lifetime of the data, and there is no code path that can erase audit entries in a running system.
