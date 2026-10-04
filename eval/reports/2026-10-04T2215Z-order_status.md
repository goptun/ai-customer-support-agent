# Evaluation: order_status — 2026-10-04

- Started at: 2026-10-04T22:15:33.562107+00:00
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
| Hallucination rate (11 judged turns) | 9% |
| Escalation rate, appropriate | 10% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 1978 | 10788 |
| `act` | 20 | 10 | 10524 |
| `classify_intent` | 23 | 942 | 1230 |
| `escalate` | 2 | 16 | 18 |
| `resolve_context` | 22 | 2 | 6 |
| `respond` | 23 | 844 | 1190 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delayed` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 1/1 flagged |
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

- `lost` turn 1: unsupported claims ['Por isso o rastreio parou de atualizar.']

## Responder fallbacks

Turns whose LLM reply was discarded for the facts, with the checks it failed.

- `delayed` turn 1: grounded: 15 dias

  > Olá! Seu pedido **ORD-2002** já foi enviado, mas está com atraso. A entrega estava prevista para **17/08/2026**, e atualmente está **15 dias atrasada**. A transportadora (Correios Mock) informou que o atraso se deve a um problema operacional, com a última atualização em **16/08/2026** (Curitiba/PR). 
  >
  > O código de rastreio é **BR200000002**. Continuamos acompanhando a situação e avisaremos assim que houver novidades. Obrigado pela paciência!

- `not_dispatched_processing` turn 1: grounded: enviado

  > Olá! O pedido ORD-1003 ainda está em separação e ainda não foi enviado à transportadora. A previsão de entrega é 08/09/2026. Qualquer outra dúvida, estou à disposição.

- `not_dispatched_paid` turn 1: grounded: enviado

  > Olá! O seu pedido **ORD‑2001** ainda não foi enviado à transportadora, por isso ainda não há código de rastreio disponível. A previsão de entrega é **07/09/2026**. Assim que houver atualização, lhe informaremos. 😊

- `pending_payment` turn 1: grounded: enviado

  > Olá! O seu pedido ORD‑3001 ainda está aguardando pagamento e, por isso, ainda não foi enviado à transportadora. Como a encomenda não foi despachada, ainda não há código de rastreamento disponível. Qualquer dúvida, estou à disposição!

- `single_recent_order` turn 1: grounded: enviado

  > Olá! Seu pedido (ORD-4001) está em separação e ainda não foi enviado à transportadora. A previsão de entrega é para o dia 06/09/2026. Qualquer outra dúvida, estou à disposição.

- `focused_order_follow_up` turn 2: complete: BR200000002

  > A data prevista de entrega do pedido ORD-2002 era **17/08/2026**.


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
