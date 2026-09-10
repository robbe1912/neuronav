// issue #20 V5: a lone struct is the file's class surface when no class
// specifier has a body.
struct Point {
	float x = 0.0f;

	float len() const { return x < 0.0f ? -x : x; }
};
