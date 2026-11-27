// C++ reference for lib_valsig.expected_quorum: g++ -O2 -o q valsig/quorum_ref.cpp && ./q > tests/fixtures/valsig/quorum-cpp.txt
#include <cmath>
#include <cstdio>
#include <algorithm>
#include <cstddef>
// same expression as postfiatd ValidatorList::calculateQuorum (return statement)
static std::size_t q(std::size_t unlSize, std::size_t effectiveUnlSize) {
    return static_cast<std::size_t>(std::max(
        std::ceil(effectiveUnlSize * 0.67f), std::ceil(unlSize * 0.6f)));
}
int main() {
    for (std::size_t n = 1; n <= 120; ++n)
        for (std::size_t e = 0; e <= n; ++e)
            std::printf("%zu %zu %zu\n", n, e, q(n, e));
}
