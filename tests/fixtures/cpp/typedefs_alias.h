// issue #20 T7: typedef / using-alias to a repo class -> alias edge to the
// defining file (cheap "type genuinely used" signal).
//- include provider.h
#include "provider.h"

//- alias ProviderAlias = Provider
typedef Provider ProviderAlias;
//- alias NameMap = int
using NameMap = int;
