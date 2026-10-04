# Evaluation: knowledge_base — 2026-10-04

- Started at: 2026-10-04T15:50:30.346375+00:00
- Models (openai_compatible): classifier `arsenal-vps`, responder `arsenal-vps`, judge `arsenal-vps`
- Commit: `81601d4`
- Conversations: 9 (9 Customer turns)

## Metrics

| Metric | Value |
|---|---|
| Task completion | 100% |
| Conversations failed to run | 0 |
| Tool-call accuracy (per turn) | 100% |
| Intent accuracy (per turn) | 100% |
| Hallucination rate (3 judged turns) | 0% |
| Escalation rate, appropriate | 0% |
| Escalation rate, avoidable | 0% |
| Escalations missed | 0 |

## Latency (ms)

| Stage | n | p50 | p95 |
|---|---|---|---|
| turn (end to end) | 9 | 1632 | 3912 |
| `classify_intent` | 9 | 850 | 2790 |
| `consult_knowledge_base` | 9 | 38 | 71 |
| `respond` | 9 | 719 | 1034 |

## Conversations

| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |
|---|---|---|---|---|---|---|
| `faq_exchange_window` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `faq_right_of_withdrawal` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `faq_free_shipping` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `faq_refund_timing` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `faq_warranty` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `faq_boleto_expiry` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `faq_support_hours` | resolved | resolved | yes | 1/1 | 1/1 | 0/1 flagged |
| `faq_miss_physical_store` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |
| `faq_miss_loyalty_program` | resolved | resolved | yes | 1/1 | 1/1 | 0/0 flagged |

## Issues

None.

## Human review sample

Check the judge: tick each verdict you agree with, note the ones you don't.

### `faq_support_hours`

**Customer:** Qual o horário de atendimento dos atendentes humanos?

**Agent:**

> Segundo a **Política de Atendimento — Canais e horários**, os atendentes humanos atendem de segunda a sexta, das 8h às 20h, e aos sábados, das 9h às 14h, exceto em feriados nacionais. 😊

<details><summary>Tool outputs</summary>

```json
[
  {
    "name": "search_knowledge_base",
    "args": {
      "query": "Qual o horário de atendimento dos atendentes humanos?"
    },
    "output": {
      "kind": "knowledge_base",
      "chunks": [
        {
          "title": "Política de Atendimento",
          "section": "Canais e horários",
          "content": "O assistente virtual atende 24 horas por dia. Os atendentes humanos atendem de segunda a sexta, das 8h às 20h, e aos sábados, das 9h às 14h, exceto feriados nacionais.",
          "score": 0.6408235214096643
        },
        {
          "title": "Política de Atendimento",
          "section": "Chamados",
          "content": "Quando o assistente não consegue resolver a sua solicitação, ele abre um chamado para um atendente com o resumo da conversa. O primeiro retorno de um atendente acontece em até 1 dia útil.",
          "score": 0.616117693779437
        },
        {
          "title": "Política de Entrega",
          "section": "Prazos de entrega",
          "content": "O prazo de entrega é informado no checkout e no e-mail de confirmação do pedido. Ele começa a contar a partir da confirmação do pagamento. Para capitais e regiões metropolitanas do Sul e do Sudeste, o prazo costuma ser de 3 a 7 dias úteis. Para as demais regiões, de 7 a 15 dias úteis.",
          "score": 0.6114040084835987
        }
      ]
    }
  }
]
```

</details>

- [ ] Judge: grounded
