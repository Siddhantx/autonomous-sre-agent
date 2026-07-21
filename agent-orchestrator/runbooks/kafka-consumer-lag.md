# Kafka consumer lag (payment pipeline)

## Symptoms
- `payment-processors` group lag on `order-events` >= 1000 messages and
  climbing.
- Payments settle late; order status stuck at `pending_payment`.

## Diagnosis
1. `kafka_consumer_lag` — is lag rising, flat, or draining?
2. `kafka_topic_desc` — did partition end offsets jump (producer burst) or
   did consumption stop (consumer crash / rebalance loop)?
3. Correlate with payment-service CPU/memory and its logs; a poison-pill
   message shows as a tight crash-restart loop with lag pinned on one
   partition.

## Remediation
- **No automatic remediation is whitelisted.** Lag is a symptom: skipping
  messages loses payments, adding consumers needs capacity review.

## Escalate when
- Always — state whether it is a throughput problem (burst, slow consumer)
  or a stuck partition (poison pill), and name the partition. A human
  decides between scaling the group and quarantining the message.
