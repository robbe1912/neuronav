<?php

namespace App\Tests;

use App\Greeter;
use App\Other\Widget;

final class GreeterTest
{
    #[Test]
    public function greets_loudly(): void
    {
        $g = new Greeter(new Widget('t'));
        assert($g->greet('x') === 'x');
    }

    #[DataProvider('cases')]
    public function greets_cases(string $n): void
    {
        $g = new Greeter(new Widget('t'));
        $g->greet($n);
    }

    /**
     * @test
     */
    public function annotated_test(): void
    {
        $g = new Greeter(new Widget('t'));
        $g->greet('a');
    }

    public function not_a_test(): void
    {
        // no attribute, no @test: dead-eligible
    }
}
