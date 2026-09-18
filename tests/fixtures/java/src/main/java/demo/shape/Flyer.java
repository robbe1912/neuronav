package demo.shape;

public interface Flyer {
    default void fly() {
        System.out.println("flying");
    }
}
