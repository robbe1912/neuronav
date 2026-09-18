package demo.shape;

public class Circle implements Shape, Flyer {
    private final int r;

    public Circle(int r) {
        this.r = r;
    }

    @Override
    public double area() {
        return 3.14 * r * r;
    }

    @Override
    public void fly() {
        System.out.println("circle flies r=" + r);
    }
}
