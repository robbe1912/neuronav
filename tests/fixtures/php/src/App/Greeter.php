<?php

namespace App;

use App\Other\Widget;
use function App\Fns\shout;

final class Greeter implements Greets
{
    use LogTrait;

    private Widget $w;

    public function __construct(Widget $w)
    {
        $this->w = $w;
    }

    public function greet(string $n): string
    {
        $this->log("greeting $n");
        Widget::describe($n);
        return $n;
    }

    public function widgetCount(): int
    {
        $other = new Widget('count');
        return $other->size();
    }

    public function viaImport(string $s): string
    {
        return shout($s);
    }
}
