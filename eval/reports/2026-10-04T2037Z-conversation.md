# Evaluation: conversation — 2026-10-04

- Started at: 2026-10-04T20:37:05.558983+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `750d7a1-dirty`
- Conversations: 11 (21 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (5 judged turns) | 0% |
| Escalation rate, appropriate | 36% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 21 | 1348 | 3567 |
| `act` | 10 | 14 | 1519 |
| `await_confirmation` | 2 | 306 | 580 |
| `cancel` | 1 | 26 | 26 |
| `classify_intent` | 20 | 1096 | 2255 |
| `consult_knowledge_base` | 2 | 39 | 40 |
| `escalate` | 4 | 14 | 15 |
| `resolve_context` | 10 | 2 | 2 |
| `respond` | 21 | 2 | 1401 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `multi_intent_same_order` | resolved | resolved | yes | 3/3 | 3/3 | 0/0 flagged |
| `multi_intent_two_orders` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `multi_intent_status_and_policy` | resolved | resolved | yes | 2/2 | 2/2 | 0/2 flagged |
| `multi_intent_refund_and_policy` | resolved | resolved | yes | 2/2 | 2/2 | 0/0 flagged |
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

### `clarification_failed_escalates`

After two Clarifications in a row, the third unclear message opens a Ticket.

**Customer:** hmm

**Agent:**

> Não tenho certeza se entendi o que você precisa. Pode me explicar com outras palavras? Por exemplo: saber onde está um pedido ou tirar uma dúvida sobre trocas.

_No tool output: not judged._

**Customer:** aquilo

**Agent:**

> Não tenho certeza se entendi o que você precisa. Pode me explicar com outras palavras? Por exemplo: saber onde está um pedido ou tirar uma dúvida sobre trocas.

_No tool output: not judged._

**Customer:** sei lá, o negócio

**Agent:**

> Desculpe, não consegui entender o que você precisa, então vou te passar para um atendente. Abri o chamado #1 com o resumo da nossa conversa, então você não vai precisar repetir nada. Um atendente vai continuar o atendimento por aqui assim que possível.

_Template reply: not judged._
