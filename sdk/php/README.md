# AutoNXT PHP SDK

Requires PHP 8.1+, `ext-curl`, `ext-json`.

```php
$client = new \AutoNXT\Client('ak_live_...', 'https://your-api-host/api/v1');

$customer = $client->customers->create(['name' => 'Jane', 'phone' => '+919800000000']);
$call = $client->calls->create([
    'direction' => 'outbound', 'to_number' => $customer['phone'],
    'from_number' => '+14155550100', 'ai_agent_id' => '<agent id>',
]);                                                   // optional 2nd arg: idempotency key
$calls = $client->calls->list(['limit' => 20, 'status' => 'completed']);
$transcript = $client->calls->transcript($call['id']);

$overview = $client->analytics->overview();
$campaign = $client->campaigns->create(['name' => 'Renewals', 'channel' => 'voice', 'ai_agent_id' => '<agent id>']);
$client->campaigns->addContacts($campaign['id'], ['customer_ids' => [$customer['id']]]);
$client->campaigns->launch($campaign['id']);

$agents = $client->aiAgents->list();
$hook = $client->webhooks->create(['url' => 'https://example.com/hook', 'events' => ['call.ended']]); // secret shown once
```

Errors throw `\AutoNXT\ApiException` (`status`, `detail`).

## Verifying webhooks

```php
$event = \AutoNXT\Webhook::constructEvent($secret, getallheaders(), file_get_contents('php://input'));
```

Signature = hex(HMAC-SHA256(secret, "{X-Timestamp}.{raw body}")) in `X-Signature`; stale timestamps
(> 5 min) are rejected. Smoke test: `php tests/smoke.php`.
