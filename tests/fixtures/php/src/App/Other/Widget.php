<?php

namespace App\Other;

use App\Greeter;

final class Widget
{
    public function __construct(private string $label)
    {
    }

    public static function describe(string $n): string
    {
        return "widget for $n";
    }

    public function size(): int
    {
        return strlen($this->label);
    }

    public function spawnGreeter(): Greeter
    {
        return new Greeter($this);
    }
}
