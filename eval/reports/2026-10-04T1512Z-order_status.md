# Evaluation: order_status — 2026-10-04

- Started at: 2026-10-04T15:12:59.819756+00:00
- Models (openai_compatible): classifier `arsenal-vps`, responder `arsenal-vps`, judge `arsenal-vps`
- Commit: `5635528`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 95% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (11 judged turns) | 0% |
| Escalation rate, appropriate | 10% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 5842 | 12380 |
| `act` | 20 | 8 | 10526 |
| `classify_intent` | 23 | 2642 | 4037 |
| `escalate` | 2 | 14 | 15 |
| `resolve_context` | 22 | 1 | 7 |
| `respond` | 23 | 2720 | 6217 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delayed` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `returned` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |
| `carrier_error` | resolved | resolved | **no** | 1/1 | 0/1 | 0/1 flagged |
| `carrier_has_no_tracking` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_processing` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `not_dispatched_paid` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `pending_payment` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `single_recent_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order_then_answer` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `focused_order_follow_up` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `unknown_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 0/0 flagged |

## Issues

- `carrier_error` turn 1: missing facts ['transportadora']

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `pending_payment`

**Customer:** Meu pedido ORD-3001 não tem rastreio, por quê?

**Agent:**

> Pedido ORD-3001: aguardando pagamento.
> Ele ainda não foi enviado à transportadora.

_Template reply: not judged._

### `single_recent_order`

No order number, but the Customer has a single recent Order.

**Customer:** Oi, quando chega meu pedido?

**Agent:**

> Encontrei um pedido recente seu, o ORD-4001.
> Pedido ORD-4001: em separação.
> Ele ainda não foi enviado à transportadora.
> Previsão de entrega: 06/09/2026.

_Template reply: not judged._
