#ifndef TEST_HEADER
#define TEST_HEADER

#include <cstdint>

class Test
{
    const char* _name;
    uint32_t    _priority;

public:
    Test( const char* name, uint32_t priority );
    void run();
};

#endif