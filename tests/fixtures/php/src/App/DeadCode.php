<?php

namespace App;

final class DeadCode
{
    public function never_called(): int
    {
        return 1;
    }

    public function __toString(): string
    {
        return 'dead';
    }
}
