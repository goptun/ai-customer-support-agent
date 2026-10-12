# Evaluation: order_status — 2026-10-12

- Started at: 2026-10-12T14:53:00.560855+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `dd0ea39`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 95% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (16 judged turns) | 0% |
| Escalation rate, appropriate | 10% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 6551 | 13228 |
| `act` | 20 | 9 | 10529 |
| `classify_intent` | 23 | 3059 | 5880 |
| `escalate` | 2 | 16 | 19 |
| `resolve_context` | 22 | 2 | 8 |
| `respond` | 23 | 2924 | 6253 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `in_transit` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `in_transit_number_only` | resolved | resolved | **no** | 1/1 | 0/1 | 0/1 flagged |
| `delayed` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `delivered_long_ago` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `lost` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `returned` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/1 flagged |
| `carrier_error` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `carrier_has_no_tracking` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_processing` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_paid` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `pending_payment` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `single_recent_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order_then_answer` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `focused_order_follow_up` | resolved | resolved | yes | 2/2 | 2/2 | 0/2 flagged |
| `someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `unknown_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 0/0 flagged |

## Issues

- `in_transit_number_only` turn 1: missing facts ['em trânsito | a caminho']

## Responder fallbacks

Turns whose LLM reply was discarded for the facts, with the checks it failed.

- `returned` turn 1: complete: devolução | devolvid

  > Olá! O código de rastreio do seu pedido é BR200000004 e a previsão de entrega era 27/08/2026. Qualquer dúvida, estou à disposição!


## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `pending_payment`

**Customer:** Meu pedido ORD-3001 não tem rastreio, por quê?

**Agent:**

> Olá! O pedido ORD-3001 está aguardando pagamento e ele ainda não foi enviado à transportadora.

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
> Olá! Tudo bem? 
>
> O seu pedido ORD-4001 está em separação e ainda não foi enviado à transportadora. A previsão de entrega é 06/09/2026. 
>
> Estou à disposição!

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
