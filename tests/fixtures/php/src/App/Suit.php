<?php

namespace App;

enum Suit
{
    case Hearts;
    case Spades;

    public function color(): string
    {
        return $this === Suit::Hearts ? 'red' : 'black';
    }
}
