<?php

declare(strict_types=1);

// Run: php tests/smoke.php
require __DIR__ . '/../src/ApiException.php';
require __DIR__ . '/../src/Webhook.php';
require __DIR__ . '/../src/Resource.php';
require __DIR__ . '/../src/Client.php';

use AutoNXT\ApiException;
use AutoNXT\Client;
use AutoNXT\Webhook;

function check(bool $cond, string $msg): void
{
    if (!$cond) {
        fwrite(STDERR, "FAIL: $msg\n");
        exit(1);
    }
}

$seen = [];
$client = new Client('ak_test_x', 'https://api.test/api/v1/', 30.0, function ($m, $url, $h, $body) use (&$seen) {
    $seen[] = [$m, $url, $h, $body];
    if (str_contains($url, '/missing')) {
        return [404, '{"detail":"Customer not found"}'];
    }
    return [200, '[{"id":"1"}]'];
});

$client->customers->list(['limit' => 5]);
check($seen[0][1] === 'https://api.test/api/v1/customers?limit=5', 'list url');
check($seen[0][2]['Authorization'] === 'Bearer ak_test_x', 'auth header');
$client->calls->create(['direction' => 'outbound']);
check(isset($seen[1][2]['Idempotency-Key']) && $seen[1][3] === '{"direction":"outbound"}', 'call create');
$client->campaigns->addContacts('c1', ['customer_ids' => ['u1']]);
check($seen[2][1] === 'https://api.test/api/v1/campaigns/c1/contacts', 'campaign contacts url');
try {
    $client->customers->get('missing');
    check(false, 'should throw');
} catch (ApiException $e) {
    check($e->status === 404 && $e->detail === 'Customer not found', 'error mapping');
}

$body = '{"event":"call.ended"}';
$ts = time();
$sig = hash_hmac('sha256', "$ts.$body", 'whsec_x');
check(Webhook::verifySignature('whsec_x', $ts, $body, $sig), 'valid signature');
check(!Webhook::verifySignature('whsec_x', $ts, $body . ' ', $sig), 'tampered body');
check(!Webhook::verifySignature('whsec_x', $ts - 1000, $body, hash_hmac('sha256', ($ts - 1000) . ".$body", 'whsec_x')), 'stale');
check(Webhook::constructEvent('whsec_x', ['X-Timestamp' => (string) $ts, 'X-Signature' => $sig], $body)['event'] === 'call.ended', 'construct');
echo "ok\n";
