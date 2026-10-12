# Customer Support Agent

An AI agent that resolves customer requests about their orders (status, cancellation, refund, policy questions) end to end, escalating to a human when it can't.

## Language

### People

**Customer**:
The authenticated person talking to the agent; owns Orders and can only act on their own.
_Avoid_: User, client, account

**Attendant**:
A human support agent who decides Approval Requests and handles Tickets.
_Avoid_: Operator, admin, human agent

### Orders

**Order**:
A purchase placed by a Customer, with items, total paid and a fulfillment status.
_Avoid_: Purchase, transaction

**Shipment**:
The physical delivery of an Order as tracked by the logistics carrier, identified by a tracking code.
_Avoid_: Delivery, package, tracking

**Dispatched**:
An Order is Dispatched once its Shipment has been handed to the carrier; before that it can still be Cancelled.
_Avoid_: Shipped (as a check), sent

### Actions

**Cancellation**:
Ending a whole Order before it is Dispatched, always after the Customer's explicit confirmation; if paid, it implies a full Refund.
_Avoid_: Order cancel, void, partial cancellation

**Refund**:
Money returned to a Customer for an Order, full or partial, never exceeding total paid minus prior Refunds.
_Avoid_: Reimbursement, chargeback

**Refund Policy**:
The deterministic rules that decide whether an Order is eligible for a Refund and for how much.
_Avoid_: Refund rules, eligibility check

**Approval Request**:
A pending request for an Attendant to approve or reject a Refund the agent has found eligible.
_Avoid_: Pending request, approval, review

**Ticket**:
A hand-off of a Conversation to an Attendant when the agent cannot or should not resolve it.
_Avoid_: Case, escalation

**Audit Log**:
The append-only record of every side effect (Cancellation, Refund, Approval Request decision, Ticket), with actor, action, inputs, result and timestamp.
_Avoid_: History, event log

### Conversation

**Conversation**:
The exchange of messages between one Customer and the agent within a session.
_Avoid_: Chat, session, thread

**Intent**:
What the Customer wants in a given message: order status, cancel order, refund, FAQ or other.
_Avoid_: Request type, category

**Focused Order**:
The Order the Conversation is currently about, carried across messages.
_Avoid_: Current order, active order

**Clarification**:
A question the agent asks instead of guessing when it is not confident about the Intent or the Order, or can't tell what the Customer wants.
_Avoid_: Disambiguation, follow-up

**Knowledge Base**:
The written policies (delivery, exchange, refund) the agent cites when answering.
_Avoid_: FAQ, docs

### Demo

**Demo Visitor**:
An anonymous person trying the public demo, given a fictitious Customer and a fictitious Attendant of their own; they chat as that Customer and decide only that Customer's Approval Requests, and nothing they do reaches a real human. A new Demo Visitor starts when their access expires or after a Demo Reset.
_Avoid_: Recruiter, guest, test user, demo session

**Demo Reset**:
The daily rebuild of all demo data from scratch to its initial seeded state, discarding every Demo Visitor along with their Conversations, actions and Audit Log.
_Avoid_: Cleanup, wipe
