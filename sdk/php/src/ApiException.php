<?php

declare(strict_types=1);

namespace AutoNXT;

class ApiException extends \RuntimeException
{
    public function __construct(public readonly int $status, public readonly mixed $detail)
    {
        parent::__construct("HTTP {$status}: " . (is_string($detail) ? $detail : json_encode($detail)));
    }
}
