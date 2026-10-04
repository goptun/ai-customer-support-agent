# Refunds caused by a Cancellation skip Attendant approval

Every other Refund needs an Approval Request, but a full Refund of an Order Cancelled before dispatch is issued automatically. The case is unambiguous (nothing left the warehouse, the amount is the total paid), the Customer already confirmed the Cancellation explicitly, and the Refund follows deterministically from the Cancellation in code, not from an LLM decision. It is still idempotent and audited like any other side effect.
