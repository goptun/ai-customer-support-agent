# Evaluation: conversation — 2026-10-04

- Started at: 2026-10-04T15:10:16.440910+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `5635528`
- Conversations: 10 (19 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 50% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 89% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (4 judged turns) | 0% |
| Escalation rate, appropriate | 30% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 1 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 19 | 3391 | 13128 |
| `act` | 9 | 11 | 1519 |
| `await_confirmation` | 2 | 548 | 1041 |
| `cancel` | 1 | 19 | 19 |
| `classify_intent` | 18 | 2963 | 5525 |
| `consult_knowledge_base` | 1 | 27 | 27 |
| `escalate` | 3 | 14 | 16 |
| `resolve_context` | 9 | 1 | 2 |
| `respond` | 19 | 2 | 6834 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `multi_intent_same_order` | resolved | resolved | yes | 3/3 | 3/3 | 0/0 flagged |
| `multi_intent_two_orders` | resolved | resolved | **no** | 1/2 | 1/2 | 0/1 flagged |
| `multi_intent_status_and_policy` | resolved | resolved | **no** | 2/2 | 1/2 | 0/1 flagged |
| `clarification` | clarification | clarification | **no** | 1/1 | 0/1 | 0/0 flagged |
| `clarification_then_understood` | resolved | resolved | **no** | 2/2 | 1/2 | 0/1 flagged |
| `clarification_failed_escalates` | escalated_appropriate | clarification | **no** | 2/3 | 0/3 | 0/0 flagged |
| `small_talk` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested_first` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested_after_refusal` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/0 flagged |
| `carrier_error_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |

## Issues

- `multi_intent_two_orders` turn 2: tool calls create_refund({"order_id": "ORD-1002", "amount_cents": null, "reason": "Ok, pode seguir com o reembolso"}), expected create_refund({"order_id": "ORD-1001"})
- `multi_intent_two_orders` turn 2: missing facts ['R$ 249,90', 'aguardando aprovação']
- `multi_intent_status_and_policy` turn 2: missing facts ['30 dias']
- `clarification` turn 1: missing facts ['outras palavras']
- `clarification_then_understood` turn 1: missing facts ['outras palavras']
- `clarification_failed_escalates`: expected escalated_appropriate, observed clarification
- `clarification_failed_escalates` turn 1: missing facts ['outras palavras']
- `clarification_failed_escalates` turn 2: missing facts ['outras palavras']
- `clarification_failed_escalates` turn 3: tool calls none, expected create_support_ticket({"reason": "clarification_failed"})
- `clarification_failed_escalates` turn 3: missing facts ['chamado']

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `small_talk`

Out of scope; the agent says what it can help with, calling no tool.

**Customer:** Oi! Você sabe me dizer se vai chover amanhã?

**Agent:**

> Sou o assistente de atendimento da loja. Por enquanto consigo te ajudar a acompanhar seus pedidos (me diga o número do pedido e eu verifico o status e a entrega), cancelar pedidos que ainda não foram enviados, solicitar e acompanhar reembolsos e tirar dúvidas sobre as políticas da loja, como prazos de entrega, trocas e reembolsos. Se preferir, também posso te passar para um atendente.

_No tool output: not judged._
