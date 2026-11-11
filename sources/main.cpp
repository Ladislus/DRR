#include "Test.hpp"

#include <iostream>

/**
 * @satisfies: REQ_00001_a_1 "Implements the 'blabla' part"
 * @satisfies: REQ_00002_a_1 "Implements the 'other' part"
 */
void print_usage()
{
    (void) 1;
}

/** @verifies: REQ_00001_a_1 */
void test()
{
    (void)1;
}

int main()
{
    Test t { "TC_01", 1 };
    ::std::cout << "Hello, World !" << ::std::endl;
    return EXIT_SUCCESS;
}
