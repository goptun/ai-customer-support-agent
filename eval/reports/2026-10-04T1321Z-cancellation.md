# Evaluation: cancellation — 2026-10-04

- Started at: 2026-10-04T13:21:31.710644+00:00
- Models (openai_compatible): classifier `router-model`, responder `router-model`, judge `router-model`
- Commit: `25403c5`
- Conversations: 9 (14 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (12 judged turns) | 42% |
| Escalation rate, appropriate | 0% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 14 | 868 | 2067 |
| `act` | 9 | 10 | 14 |
| `await_confirmation` | 10 | 274 | 688 |
| `cancel` | 3 | 20 | 25 |
| `classify_intent` | 10 | 877 | 1431 |
| `resolve_context` | 10 | 2 | 7 |
| `respond` | 14 | 2 | 264 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `cancel_paid_confirmed` | resolved | resolved | yes | 2/2 | 2/2 | 1/2 flagged |
| `cancel_unpaid_confirmed` | resolved | resolved | yes | 2/2 | 2/2 | 0/2 flagged |
| `cancel_denied` | resolved | resolved | yes | 2/2 | 2/2 | 1/1 flagged |
| `cancel_dropped_by_other_message` | resolved | resolved | yes | 2/2 | 2/2 | 1/2 flagged |
| `cancel_single_recent_order` | resolved | resolved | yes | 2/2 | 2/2 | 1/2 flagged |
| `cancel_which_order` | clarification | clarification | yes | 1/1 | 1/1 | 0/0 flagged |
| `cancel_already_dispatched` | resolved | resolved | yes | 1/1 | 1/1 | 1/1 flagged |
| `cancel_already_cancelled` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `cancel_someone_elses_order` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |

## Issues

- `cancel_paid_confirmed` turn 1: unsupported claims ['ele já está pago', 'reembolso integral de R$\xa0399,00', 'o reembolso será feito automaticamente']
- `cancel_denied` turn 1: unsupported claims ['Como ele já está pago, o reembolso integral de R$ 119,80 será feito automaticamente']
- `cancel_dropped_by_other_message` turn 1: unsupported claims ['Ele já está pago', 'O reembolso será feito automaticamente', 'O pedido ORD-4001 será cancelado inteiro (integral)', 'O reembolso é integral de R$ 89,90']
- `cancel_single_recent_order` turn 1: unsupported claims ['Como ele já está pago, o reembolso integral de R$ 89,90 será feito automaticamente.']
- `cancel_already_dispatched` turn 1: unsupported claims ['Você pode recusar a entrega quando ela chegar ou, depois de receber, solicitar a devolução (Política de Cancelamento; Política de Devolução e Direito de Arrependimento).']

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `cancel_already_dispatched`

Already with the carrier; the agent offers refusing the delivery or a return.

**Customer:** Quero cancelar o ORD-1002, comprei errado

**Agent:**

> O pedido ORD-1002 já foi enviado à transportadora, então não pode mais ser cancelado. Você pode recusar a entrega quando ela chegar ou, depois de receber, solicitar a devolução (Política de Cancelamento; Política de Devolução e Direito de Arrependimento).

<details><summary>Tool outputs</summary>

```json
[
  {
    "name": "preview_cancellation",
    "args": {
      "order_id": "ORD-1002"
    },
    "output": {
      "kind": "error",
      "code": "already_dispatched",
      "order_id": "ORD-1002"
    }
  }
]
```

</details>

- [ ] Judge: hallucinated — ['Você pode recusar a entrega quando ela chegar ou, depois de receber, solicitar a devolução (Política de Cancelamento; Política de Devolução e Direito de Arrependimento).']
