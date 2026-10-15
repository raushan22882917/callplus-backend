<?php

declare(strict_types=1);

namespace AutoNXT;

/**
 * Client for the AutoNXT public API.
 *
 *   $client = new \AutoNXT\Client('ak_live_...', 'https://api.example.com/api/v1');
 *   $client->customers->create(['name' => 'Jane', 'phone' => '+919800000000']);
 */
final class Client
{
    public Resource $calls;
    public Resource $customers;
    public Resource $campaigns;
    public Resource $analytics;
    public Resource $aiAgents;
    public Resource $webhooks;

    /** @var callable|null  fn(string $method, string $url, array $headers, ?string $body): array{int,string} */
    private $transport;

    public function __construct(
        private readonly string $apiKey,
        private readonly string $baseUrl = 'http://localhost:8000/api/v1',
        private readonly float $timeout = 30.0,
        ?callable $transport = null
    ) {
        if ($apiKey === '') {
            throw new \InvalidArgumentException('apiKey is required');
        }
        $this->transport = $transport;
        $this->calls = new Resource($this, [
            'list' => ['GET', '/calls'],
            'get' => ['GET', '/calls/{id}'],
            'create' => ['POST', '/calls'],
            'transcript' => ['GET', '/calls/{id}/transcript'],
        ]);
        $this->customers = new Resource($this, [
            'list' => ['GET', '/customers'],
            'get' => ['GET', '/customers/{id}'],
            'create' => ['POST', '/customers'],
            'update' => ['PATCH', '/customers/{id}'],
            'delete' => ['DELETE', '/customers/{id}'],
        ]);
        $this->campaigns = new Resource($this, [
            'list' => ['GET', '/campaigns'],
            'get' => ['GET', '/campaigns/{id}'],
            'create' => ['POST', '/campaigns'],
            'addContacts' => ['POST', '/campaigns/{id}/contacts'],
            'contacts' => ['GET', '/campaigns/{id}/contacts'],
            'launch' => ['POST', '/campaigns/{id}/launch'],
            'pause' => ['POST', '/campaigns/{id}/pause'],
            'resume' => ['POST', '/campaigns/{id}/resume'],
        ]);
        $this->analytics = new Resource($this, [
            'overview' => ['GET', '/analytics/overview'],
            'calls' => ['GET', '/analytics/calls'],
            'tickets' => ['GET', '/analytics/tickets'],
            'campaigns' => ['GET', '/analytics/campaigns'],
        ]);
        $this->aiAgents = new Resource($this, [
            'list' => ['GET', '/ai-agents'],
            'get' => ['GET', '/ai-agents/{id}'],
            'create' => ['POST', '/ai-agents'],
            'update' => ['PATCH', '/ai-agents/{id}'],
        ]);
        $this->webhooks = new Resource($this, [
            'list' => ['GET', '/developer/webhooks'],
            'create' => ['POST', '/developer/webhooks'],
            'update' => ['PATCH', '/developer/webhooks/{id}'],
            'delete' => ['DELETE', '/developer/webhooks/{id}'],
            'test' => ['POST', '/developer/webhooks/{id}/test'],
            'deliveries' => ['GET', '/developer/webhooks/deliveries'],
        ]);
    }

    /** @return mixed decoded JSON (null for empty responses) */
    public function request(string $method, string $path, array $query = [], ?array $body = null, array $headers = []): mixed
    {
        $url = rtrim($this->baseUrl, '/') . $path;
        $query = array_filter($query, static fn ($v) => $v !== null);
        if ($query) {
            $url .= '?' . http_build_query($query);
        }
        $all = array_merge([
            'Authorization' => "Bearer {$this->apiKey}",
            'Accept' => 'application/json',
            'User-Agent' => 'autonxt-php/0.1.0',
        ], $headers);
        $payload = null;
        if ($body !== null) {
            $payload = json_encode($body, JSON_THROW_ON_ERROR);
            $all['Content-Type'] = 'application/json';
        }
        [$status, $raw] = $this->transport
            ? ($this->transport)($method, $url, $all, $payload)
            : $this->curl($method, $url, $all, $payload);
        $data = $raw === '' ? null : json_decode($raw, true);
        if ($status >= 400) {
            throw new ApiException($status, is_array($data) ? ($data['detail'] ?? $data) : $raw);
        }
        return $data;
    }

    private function curl(string $method, string $url, array $headers, ?string $payload): array
    {
        $ch = curl_init($url);
        $lines = [];
        foreach ($headers as $k => $v) {
            $lines[] = "$k: $v";
        }
        curl_setopt_array($ch, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $lines,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => (int) $this->timeout,
            CURLOPT_POSTFIELDS => $payload,
        ]);
        $raw = curl_exec($ch);
        if ($raw === false) {
            $error = curl_error($ch);
            curl_close($ch);
            throw new \RuntimeException("AutoNXT request failed: $error");
        }
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        return [$status, (string) $raw];
    }
}
