<?php

namespace App\Fns;

function shout(string $s): string
{
    return strtoupper($s);
}

function whisper(string $s): string
{
    return strtolower($s);
}
