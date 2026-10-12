# Evaluation: conversation — 2026-10-04

- Started at: 2026-10-04T17:57:49.223905+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `66d44a9-dirty`
- Conversations: 10 (19 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (5 judged turns) | 0% |
| Escalation rate, appropriate | 40% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 19 | 1285 | 3617 |
| `act` | 9 | 13 | 1521 |
| `await_confirmation` | 2 | 300 | 569 |
| `cancel` | 1 | 25 | 25 |
| `classify_intent` | 18 | 970 | 1708 |
| `consult_knowledge_base` | 1 | 43 | 43 |
| `escalate` | 4 | 14 | 18 |
| `resolve_context` | 9 | 2 | 2 |
| `respond` | 19 | 2 | 1489 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `multi_intent_same_order` | resolved | resolved | yes | 3/3 | 3/3 | 0/0 flagged |
| `multi_intent_two_orders` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `multi_intent_status_and_policy` | resolved | resolved | yes | 2/2 | 2/2 | 0/2 flagged |
| `clarification` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `clarification_then_understood` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `clarification_failed_escalates` | escalated_appropriate | escalated (appropriate) | yes | 3/3 | 3/3 | 0/0 flagged |
| `small_talk` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested_first` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested_after_refusal` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/0 flagged |
| `carrier_error_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |

## Issues

None.

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `small_talk`

Out of scope; the agent says what it can help with, calling no tool.

**Customer:** Oi! Você sabe me dizer se vai chover amanhã?

**Agent:**

> Sou o assistente de atendimento da loja. Por enquanto consigo te ajudar a acompanhar seus pedidos (me diga o número do pedido e eu verifico o status e a entrega), cancelar pedidos que ainda não foram enviados, solicitar e acompanhar reembolsos e tirar dúvidas sobre as políticas da loja, como prazos de entrega, trocas e reembolsos. Se preferir, também posso te passar para um atendente.

_No tool output: not judged._
