<?php

// convention entry: web front controller — every func roots
use App\Greeter;
use App\Other\Widget;

function page_main(): string
{
    $g = new Greeter(new Widget('page'));
    return $g->greet('web');
}

function page_helper(): string
{
    return Widget::describe('helper');
}
