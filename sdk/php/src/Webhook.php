<?php

declare(strict_types=1);

namespace AutoNXT;

/**
 * Verifies webhooks sent by AutoNXT: X-Signature is
 * hex(HMAC-SHA256(secret, "{X-Timestamp}.{raw body}")). Use the RAW body.
 */
final class Webhook
{
    public static function computeSignature(string $secret, string|int $timestamp, string $body): string
    {
        return hash_hmac('sha256', $timestamp . '.' . $body, $secret);
    }

    public static function verifySignature(
        string $secret,
        string|int $timestamp,
        string $body,
        string $signature,
        int $toleranceSeconds = 300,
        ?int $now = null
    ): bool {
        if (!is_numeric($timestamp)) {
            return false;
        }
        $ts = (int) $timestamp;
        if ($toleranceSeconds > 0 && abs(($now ?? time()) - $ts) > $toleranceSeconds) {
            return false;
        }
        return hash_equals(self::computeSignature($secret, $ts, $body), strtolower(trim($signature)));
    }

    /** Verify then decode; $headers keys are matched case-insensitively. */
    public static function constructEvent(string $secret, array $headers, string $body, int $toleranceSeconds = 300): array
    {
        $lowered = array_change_key_case($headers, CASE_LOWER);
        if (!self::verifySignature($secret, $lowered['x-timestamp'] ?? '', $body, $lowered['x-signature'] ?? '', $toleranceSeconds)) {
            throw new \InvalidArgumentException('Invalid or expired webhook signature');
        }
        return json_decode($body, true, 512, JSON_THROW_ON_ERROR);
    }
}
