package demo;

public class Greeter {
    private String prefix = "hi";

    public String greet(String who) {
        return prefix + " " + who;
    }

    public static String banner() {
        return "demo";
    }

    public String toString() {
        return "Greeter";
    }
}
