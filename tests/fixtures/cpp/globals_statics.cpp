// issue #20 T6: unused file-scope statics are honest dead material — they
// must surface in fs.globals; function locals must not.
static int s_counter = 0;
const int kLimit = 8;

static void bump() {
	s_counter += 1;
	int local_only = s_counter;
	(void)local_only;
}
