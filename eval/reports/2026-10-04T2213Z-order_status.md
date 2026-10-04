# Evaluation: order_status — 2026-10-04

- Started at: 2026-10-04T22:13:04.094814+00:00
- Models (openai_compatible): classifier `arsenal-vps`, responder `arsenal-vps`, judge `arsenal-vps`
- Commit: `44c3891`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
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
| turn (end to end) | 23 | 1867 | 10684 |
| `act` | 20 | 10 | 10526 |
| `classify_intent` | 23 | 850 | 2023 |
| `escalate` | 2 | 21 | 23 |
| `resolve_context` | 22 | 2 | 7 |
| `respond` | 23 | 816 | 1829 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delayed` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `returned` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |
| `carrier_error` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
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

None.

## Responder fallbacks

Turns whose LLM reply was discarded for the facts, with the checks it failed.

- `lost` turn 1: grounded: 2 dias

  > Olá! O rastreio do seu pedido ORD‑1004 (código BR100000004) indica que o objeto foi **extraviado**, com a última atualização em 29/08/2026 (Curitiba/PR). A entrega estava prevista para 30/08/2026, mas já está 2 dias atrasada em relação a essa data. 
  >
  > Posso ajudar a abrir uma solicitação de reposição ou reembolso, se desejar.

- `not_dispatched_processing` turn 1: grounded: enviado

  > Olá! O pedido ORD‑1003 ainda está em separação e não foi enviado à transportadora. A previsão de entrega é para o dia 08/09/2026. Qualquer outra dúvida, estou à disposição!

- `not_dispatched_paid` turn 1: grounded: enviado

  > Olá! O seu pedido ORD‑2001 ainda não foi enviado à transportadora, por isso ainda não há código de rastreio. A previsão de entrega é 07/09/2026. Qualquer novidade, avisaremos!

- `pending_payment` turn 1: grounded: enviado

  > Olá! O seu pedido ORD‑3001 ainda está aguardando pagamento e, por isso, ainda não foi enviado à transportadora. Enquanto isso não for confirmado, não há código de rastreamento disponível. Se precisar de ajuda para concluir o pagamento, estou à disposição!

- `single_recent_order` turn 1: grounded: enviado

  > Olá! Seu pedido está em separação e ainda não foi enviado à transportadora. A previsão de entrega é para o dia 06/09/2026.

- `focused_order_follow_up` turn 2: complete: BR200000002

  > A data prevista de entrega do pedido **ORD-2002** era **17/08/2026**.


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
