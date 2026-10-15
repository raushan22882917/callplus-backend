<?php

declare(strict_types=1);

namespace AutoNXT;

/**
 * A group of endpoints. Call by name:
 *   $client->customers->get($id)                      GET  /customers/{id}
 *   $client->customers->list(['limit' => 10])         GET  /customers?limit=10
 *   $client->customers->create([...])                 POST /customers
 *   $client->customers->update($id, [...])            PATCH
 *   $client->campaigns->addContacts($id, ['customer_ids' => [...]])
 *   $client->calls->create([...], 'idempotency-key')  (key generated if omitted)
 */
final class Resource
{
    /** @param array<string, array{string, string}> $routes */
    public function __construct(private readonly Client $client, private readonly array $routes)
    {
    }

    public function __call(string $name, array $args): mixed
    {
        if (!isset($this->routes[$name])) {
            throw new \BadMethodCallException("Unknown method {$name}");
        }
        [$method, $path] = $this->routes[$name];
        $query = [];
        $body = null;
        $headers = [];
        if (str_contains($path, '{id}')) {
            $path = str_replace('{id}', rawurlencode((string) array_shift($args)), $path);
        }
        $first = $args[0] ?? [];
        if ($method === 'GET' || $method === 'DELETE') {
            $query = is_array($first) ? $first : [];
        } else {
            $body = is_array($first) ? $first : null;
            if ($name === 'create' && $path === '/calls') {
                $headers['Idempotency-Key'] = $args[1] ?? bin2hex(random_bytes(16));
            }
        }
        return $this->client->request($method, $path, $query, $body, $headers);
    }
}
