# Evaluation: cancellation — 2026-10-04

- Started at: 2026-10-04T15:49:13.971733+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `81601d4`
- Conversations: 9 (14 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (1 judged turns) | 0% |
| Escalation rate, appropriate | 0% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 14 | 827 | 3288 |
| `act` | 9 | 10 | 13 |
| `await_confirmation` | 10 | 251 | 684 |
| `cancel` | 3 | 21 | 27 |
| `classify_intent` | 10 | 795 | 2656 |
| `resolve_context` | 10 | 2 | 9 |
| `respond` | 14 | 2 | 364 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `cancel_paid_confirmed` | resolved | resolved | yes | 2/2 | 2/2 | 0/0 flagged |
| `cancel_unpaid_confirmed` | resolved | resolved | yes | 2/2 | 2/2 | 0/0 flagged |
| `cancel_denied` | resolved | resolved | yes | 2/2 | 2/2 | 0/0 flagged |
| `cancel_dropped_by_other_message` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `cancel_single_recent_order` | resolved | resolved | yes | 2/2 | 2/2 | 0/0 flagged |
| `cancel_which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `cancel_already_dispatched` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `cancel_already_cancelled` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `cancel_someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |

## Issues

None.

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `cancel_already_dispatched`

Already with the carrier; the agent offers refusing the delivery or a return.

**Customer:** Quero cancelar o ORD-1002, comprei errado

**Agent:**

> O pedido ORD-1002 já foi enviado à transportadora, então não pode mais ser cancelado. Você pode recusar a entrega quando ela chegar ou, depois de receber, solicitar a devolução (Política de Cancelamento; Política de Devolução e Direito de Arrependimento).

_Template reply: not judged._
