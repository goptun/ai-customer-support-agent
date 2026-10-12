# Evaluation: order_status — 2026-10-04

- Started at: 2026-10-04T13:19:51.999255+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `25403c5`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 90% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (21 judged turns) | 14% |
| Escalation rate, appropriate | 10% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 1862 | 10809 |
| `act` | 20 | 9 | 10522 |
| `classify_intent` | 23 | 877 | 1472 |
| `escalate` | 2 | 20 | 21 |
| `resolve_context` | 22 | 2 | 6 |
| `respond` | 23 | 897 | 1358 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | **no** | 1/1 | 0/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delayed` | resolved | resolved | **no** | 1/1 | 0/1 | 0/1 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `returned` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 1/2 flagged |
| `carrier_error` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_has_no_tracking` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_processing` | resolved | resolved | yes | 1/1 | 1/1 | 1/1 flagged |
| `not_dispatched_paid` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `pending_payment` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `single_recent_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order_then_answer` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `focused_order_follow_up` | resolved | resolved | yes | 2/2 | 2/2 | 0/2 flagged |
| `someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `unknown_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `human_requested` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 1/1 flagged |

## Issues

- `in_transit` turn 1: missing facts ['BR100000002']
- `delayed` turn 1: missing facts ['17/08/2026']
- `carrier_timeout_escalates` turn 2: unsupported claims ['Nossos sistemas estão instáveis e não consegui concluir a consulta']
- `not_dispatched_processing` turn 1: unsupported claims ['Pedido ORD-1003: em separação.']
- `human_requested` turn 1: unsupported claims ['Um atendente vai continuar o atendimento por aqui assim que possível']

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `pending_payment`

**Customer:** Meu pedido ORD-3001 não tem rastreio, por quê?

**Agent:**

> Pedido ORD-3001: aguardando pagamento.
> Ele ainda não foi enviado à transportadora.

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
> Pedido ORD-4001: em separação.
> Ele ainda não foi enviado à transportadora.
> Previsão de entrega: 06/09/2026.

<details><summary>Tool outputs</summary>

```json
[
  {
    "name": "get_order_status",
    "args": {
      "order_id": "ORD-4001"
    },
    "output": {
      "kind": "order_status",
      "order_id": "ORD-4001",
      "status": "processing",
      "placed_at": "2026-08-30T12:00:00Z",
      "promised_delivery_date": "2026-09-06",
      "delivered_at": null,
      "days_late": 0,
      "shipment": null
    }
  }
]
```

</details>

- [ ] Judge: grounded
