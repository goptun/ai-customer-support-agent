# Evaluation: order_status — 2026-10-12

- Started at: 2026-10-12T15:00:26.292284+00:00
- Models (openai_compatible): classifier `arsenal-vps`, responder `arsenal-vps`, judge `arsenal-vps`
- Commit: `bf62441`
- Conversations: 20 (23 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (12 judged turns) | 0% |
| Escalation rate, appropriate | 10% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 23 | 2808 | 11171 |
| `act` | 20 | 10 | 10528 |
| `classify_intent` | 23 | 1176 | 2084 |
| `escalate` | 2 | 20 | 20 |
| `resolve_context` | 22 | 2 | 7 |
| `respond` | 23 | 1295 | 3213 |

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
| `carrier_timeout_escalates` | escalated_appropriate | escalated (appropriate) | yes | 2/2 | 2/2 | 0/0 flagged |
| `carrier_error` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `carrier_has_no_tracking` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_processing` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `not_dispatched_paid` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `pending_payment` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `single_recent_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `which_order_then_answer` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `focused_order_follow_up` | resolved | resolved | yes | 2/2 | 2/2 | 0/1 flagged |
| `someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `unknown_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `human_requested` | escalated_appropriate | escalated (appropriate) | yes | 1/1 | 1/1 | 0/0 flagged |

## Issues

- `not_dispatched_paid` turn 1: Judge failed: RuntimeError: judge gave no verdict: ''

## Responder fallbacks

Turns whose LLM reply was discarded for the facts, with the checks it failed.

- `lost` turn 1: grounded: 2 dias

  > Olá! 
  >
  > O rastreio do seu pedido **ORD-1004** (código BR100000004) indica que o objeto foi **extraviado**, com a última atualização em **29/08/2026** em Curitiba/PR. A previsão de entrega era **30/08/2026**, mas o pedido já está 2 dias atrasado em relação a essa data.
  >
  > Estou à disposição para ajudar no que for preciso.

- `carrier_timeout_escalates` turn 1: complete: transportadora

  > Olá! Seu pedido **ORD-3003** já foi enviado. O código de rastreio é **BR300000003** e a previsão de entrega é **04/09/2026**. No momento não consegui consultar a transportadora para obter a atualização mais recente; por favor, tente novamente em alguns minutos. 🙂

- `carrier_error` turn 1: complete: transportadora

  > Olá! Seu pedido **ORD-3004** já foi enviado. O código de rastreio é **BR300000004** e a previsão de entrega é **04/09/2026**. No momento não consegui obter a atualização mais recente da transportadora; tente consultar novamente em alguns minutos. Qualquer outra dúvida, estou à disposição!

- `focused_order_follow_up` turn 1: grounded: 15 dias

  > Olá! Seu pedido **ORD-2002** já foi enviado.  
  > - **Código de rastreio:** BR200000002  
  > - **Previsão de entrega:** 17/08/2026  
  >
  > Atualmente o pedido está **15 dias atrasado** por um problema operacional da transportadora (última atualização em 16/08/2026 – Curitiba/PR).  
  >
  > Agradecemos a paciência e estamos à disposição para ajudar no que precisar.


## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `pending_payment`

**Customer:** Meu pedido ORD-3001 não tem rastreio, por quê?

**Agent:**

> Olá! O pedido **ORD-3001** ainda está aguardando o pagamento, por isso ainda não foi enviado à transportadora. Como o envio ainda não ocorreu, não há código de rastreamento disponível. Assim que o pagamento for confirmado e o pedido for despachado, você receberá o código de rastreio e a previsão de entrega. Qualquer outra dúvida, estou à disposição!

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
> Olá! Seu pedido ainda está em fase de separação e ainda não foi enviado à transportadora. A previsão de entrega é 06/09/2026. Qualquer outra dúvida, estou à disposição.

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
