package demo;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;

public class AppTest {
    @Test
    public void greets() {
        new Greeter().greet("t");
    }

    @Test
    void bannerTest() {
        Greeter.banner();
    }

    @ParameterizedTest
    void areas() {
        new demo.shape.Circle(1).area();
    }

    void notATest() {
        // no annotation: only alive because greets() -> ? nothing calls
        // this; stays dead-eligible
    }
}
