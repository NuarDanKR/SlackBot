/**
 * Codex R1b: observed response accounting, not an invoice ledger.
 * SDK-internal retries are not observable here. Completeness only describes
 * application-visible attempts; unknown usage/rates are never treated as free.
 */
import { estimateCost } from '../format.js';

const TOKEN_KEYS = ['input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'];
const valid = (n) => Number.isSafeInteger(n) && n >= 0;
const copy = (value) => structuredClone(value);
const blank = () => Object.fromEntries(TOKEN_KEYS.map((k) => [k, 0]));

function usable(u) {
  return u && valid(u.input_tokens) && valid(u.output_tokens)
    && TOKEN_KEYS.every((k) => u[k] === undefined || valid(u[k]))
    && (!u.cache_creation || Object.values(u.cache_creation).every(valid));
}
function addUsage(total, u) {
  if (!u) return;
  for (const k of TOKEN_KEYS) total[k] += u[k] || 0;
  total.cache_creation ??= { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 };
  const five = u.cache_creation?.ephemeral_5m_input_tokens || 0;
  const hour = u.cache_creation?.ephemeral_1h_input_tokens || 0;
  total.cache_creation.ephemeral_5m_input_tokens += five;
  total.cache_creation.ephemeral_1h_input_tokens += hour + Math.max(0, (u.cache_creation_input_tokens || 0) - five - hour);
}

export function summarizeUsage(records) {
  const usage = blank();
  const byModel = new Map();
  let knownCostUsd = 0;
  let unknownAttempts = 0;
  for (const record of records) {
    addUsage(usage, record.usage);
    const row = byModel.get(record.model) || { model: record.model, usage: blank(), knownCostUsd: 0, unknownAttempts: 0 };
    addUsage(row.usage, record.usage);
    if (record.costUsd == null) { unknownAttempts++; row.unknownAttempts++; }
    else { knownCostUsd += record.costUsd; row.knownCostUsd += record.costUsd; }
    byModel.set(record.model, row);
  }
  return {
    version: 1, scope: 'application-observed-attempts', sdkRetriesObserved: false,
    records: copy(records), byModel: [...byModel.values()], usage,
    knownCostUsd, unknownAttempts, complete: unknownAttempts === 0,
    costUsd: unknownAttempts ? null : knownCostUsd,
  };
}

export function createUsageCollector({ estimate = estimateCost } = {}) {
  const records = [];
  const seen = new Set();
  return {
    record(message, requestedModel) {
      // The same SDK message must not be billed twice if observed again.
      if (message?.id && seen.has(message.id)) return;
      if (message?.id) seen.add(message.id);
      const model = message?.model || requestedModel || '(unknown)';
      const usage = usable(message?.usage) ? copy(message.usage) : null;
      const cost = usage ? estimate(model, usage) : null;
      const priced = cost?.rateKnown === true && Number.isFinite(cost.usd) && cost.usd >= 0;
      records.push({
        model, messageId: message?.id || null, stopReason: message?.stop_reason || null,
        usage, costUsd: priced ? cost.usd : null,
        rateBasis: cost?.rateBasis || null,
        unknownReason: !usage ? 'missing-or-invalid-usage' : !priced ? 'unknown-rate' : null,
      });
    },
    failed(model) {
      records.push({ model: model || '(unknown)', usage: null, costUsd: null, unknownReason: 'unobserved-attempt' });
    },
    merge(accounting) {
      if (accounting?.records) records.push(...copy(accounting.records));
    },
    snapshot() { return summarizeUsage(records); },
  };
}

/** Only accounting fields may cross the error/result -> log boundary. */
export function usageFields(accounting) {
  if (!accounting) return {};
  return {
    accounting,
    usage: accounting.usage,
    model: accounting.byModel.map((r) => r.model).join('+'),
    costUsd: accounting.costUsd,
    knownCostUsd: accounting.knownCostUsd,
  };
}

export function attachUsage(error, accounting) {
  if (error && (typeof error === 'object' || typeof error === 'function')) {
    error.hermesAccounting = accounting;
  }
  return error;
}
