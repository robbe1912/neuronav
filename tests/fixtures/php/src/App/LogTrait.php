<?php

namespace App;

trait LogTrait
{
    public function log(string $m): void
    {
        // trait method bodies live in the trait's own file
    }

    public function logLoud(string $m): void
    {
        $this->log(strtoupper($m));
    }
}
