// issue #20 T7: typedef / using-alias to a repo class -> alias edge to the
// defining file (cheap "type genuinely used" signal).
#include "provider.h"

typedef Provider ProviderAlias;
using NameMap = int;
