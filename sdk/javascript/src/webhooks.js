import { createHmac, timingSafeEqual } from "node:crypto";

/** hex(HMAC-SHA256(secret, `${timestamp}.${rawBody}`)) */
export function computeSignature(secret, timestamp, body) {
  const raw = Buffer.isBuffer(body) ? body.toString("utf8") : body;
  return createHmac("sha256", secret).update(`${timestamp}.${raw}`).digest("hex");
}

/**
 * Verify an AutoNXT webhook. Pass the RAW request body (string/Buffer), the
 * X-Timestamp and X-Signature header values. Rejects stale timestamps.
 */
export function verifySignature(secret, timestamp, body, signature, { toleranceSeconds = 300, now = Date.now() / 1000 } = {}) {
  const ts = Number.parseInt(timestamp, 10);
  if (!Number.isFinite(ts)) return false;
  if (toleranceSeconds && Math.abs(now - ts) > toleranceSeconds) return false;
  const expected = Buffer.from(computeSignature(secret, ts, body));
  const given = Buffer.from(String(signature ?? "").trim().toLowerCase());
  return expected.length === given.length && timingSafeEqual(expected, given);
}

export class SignatureVerificationError extends Error {}

/** Verify then parse; `headers` is any object with (case-insensitive) header names. */
export function constructEvent(secret, headers, body, options) {
  const lowered = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  if (!verifySignature(secret, lowered["x-timestamp"], body, lowered["x-signature"], options)) {
    throw new SignatureVerificationError("Invalid or expired webhook signature");
  }
  return JSON.parse(Buffer.isBuffer(body) ? body.toString("utf8") : body);
}
