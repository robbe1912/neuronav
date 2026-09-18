package demo;

import demo.shape.Shape;
import demo.shape.Flyer;
import static demo.Greeter.banner;

import java.util.List;

public class App {
    public static void main(String[] args) {
        Greeter g = new Greeter();
        g.greet("world");
        banner();
        Shape s = new demo.shape.Circle(2);
        s.describe();
        double a = s.area();
        Flyer f = new demo.shape.Circle(1);
        f.fly();
        helper();
        System.out.println("area " + a);
    }

    static void helper() {
        List<String> xs = null;
        System.out.println("helper " + xs);
    }
}
