# One graph thread per Conversation, a separate thread per approval workflow

`interrupt()` pauses the whole thread, so a Refund awaiting approval on the Conversation thread would block it: the Customer's next message would be taken as the resume value. Each action that needs Attendant approval therefore runs on its own thread; the Conversation keeps going, and the Attendant's decision is appended to the Conversation as an agent message the Customer sees on their next interaction (or by polling).

## Considered Options

- Single thread, Conversation blocked while an approval is pending: simpler, but a poor experience.
- Interrupt only a subgraph on the Conversation thread: LangGraph still resumes through the parent thread, so it hits the same problem.
