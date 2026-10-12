# Evaluation: order_status — 2026-10-12

- Started at: 2026-10-12T12:30:24.260903+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `ac13005`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 85% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 87% |
| Intent accuracy (per turn) | 91% |
| Hallucination rate (11 judged turns) | 0% |
| Escalation rate, appropriate | 5% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 1 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 7951 | 34588 |
| `act` | 18 | 9 | 10524 |
| `classify_intent` | 23 | 4052 | 34487 |
| `escalate` | 1 | 20 | 20 |
| `resolve_context` | 20 | 2 | 7 |
| `respond` | 23 | 2008 | 9374 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delayed` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `returned` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |
| `carrier_error` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_has_no_tracking` | resolved | clarification | **no** | 0/1 | 0/1 | 0/0 flagged |
| `not_dispatched_processing` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_paid` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `pending_payment` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `single_recent_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order_then_answer` | resolved | clarification | **no** | 1/2 | 1/2 | 0/0 flagged |
| `focused_order_follow_up` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `unknown_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested` | escalated_appropriate | clarification | **no** | 0/1 | 0/1 | 0/0 flagged |

## Issues

- `carrier_has_no_tracking`: expected resolved, observed clarification
- `carrier_has_no_tracking` turn 1: Intent other, expected order_status
- `carrier_has_no_tracking` turn 1: tool calls none, expected get_order_status({"order_id": "ORD-3005"})
- `carrier_has_no_tracking` turn 1: missing facts ['05/09/2026', 'BR300000005']
- `single_recent_order` turn 1: Judge failed: RuntimeError: judge gave no verdict: 'analysis:\n\nWe need to check claims in agent reply against tool outputs.\n\n tool output: order status processing, placed at 2026-08- 30T12: date; promised delivery date 202 format ISO 2026-09-06; status'
- `which_order_then_answer`: expected resolved, observed clarification
- `which_order_then_answer` turn 2: Intent other, expected order_status
- `which_order_then_answer` turn 2: tool calls none, expected get_order_status({"order_id": "ORD-1002"})
- `which_order_then_answer` turn 2: missing facts ['03/09/2026']
- `human_requested`: expected escalated_appropriate, observed clarification
- `human_requested` turn 1: tool calls none, expected create_support_ticket({"reason": "customer_requested"})
- `human_requested` turn 1: missing facts ['chamado']

## Responder fallbacks

Turns whose LLM reply was discarded for the facts, with the checks it failed.

- `delayed` turn 1: grounded: 15 dias

  > Olá! O pedido **ORD‑2002** já foi enviado (código de rastreio **BR200000002**). A previsão de entrega era **17/08/2026**, mas está **15 dias atrasado**. A última atualização da transportadora, em 16/08/2026 (Curitiba/PR), informa que a entrega está atrasada por problema operacional. Estamos acompanhando o caso e avisaremos assim que houver nova informação.

- `lost` turn 1: empty reply


- `focused_order_follow_up` turn 2: complete: BR200000002

  > A data prevista de entrega do pedido **ORD‑2002** era **17/08/2026**.


## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `pending_payment`

**Customer:** Meu pedido ORD-3001 não tem rastreio, por quê?

**Agent:**

> Olá! O pedido ORD‑3001 ainda está **aguardando pagamento**, por isso ainda não foi enviado à transportadora. Como o envio ainda não ocorreu, o código de rastreamento ainda não foi gerado. Assim que o pagamento for confirmado, o pedido será despachado e você receberá o rastreio. Se precisar de ajuda para concluir o pagamento, estou à disposição!

<details><summary>Tool outputs</summary>

```json
[
  {
    "name": "get_order_status",
    "args": {
      "order_id": "ORD-3001"
    },
    "output": {
      "kind": "order_status",
      "order_id": "ORD-3001",
      "status": "pending_payment",
      "placed_at": "2026-09-01T12:00:00Z",
      "promised_delivery_date": null,
      "delivered_at": null,
      "days_late": null,
      "shipment": null
    }
  }
]
```

</details>

- [ ] Judge: grounded

### `single_recent_order`

No order number, but the Customer has a single recent Order.

**Customer:** Oi, quando chega meu pedido?

**Agent:**

> Encontrei um pedido recente seu, o ORD-4001.
> Olá! Seu pedido ORD‑4001 ainda está em separação e ainda não foi enviado à transportadora. A previsão de entrega era para 06/09/2026. Como ainda não saiu para entrega, pode haver um atraso. Estamos acompanhando e avisaremos assim que houver novidades.

_Judge failed: RuntimeError: judge gave no verdict: 'analysis:\n\nWe need to check claims in agent reply against tool outputs.\n\n tool output: order status processing, placed at 2026-08- 30T12: date; promised delivery date 202 format ISO 2026-09-06; status'_
