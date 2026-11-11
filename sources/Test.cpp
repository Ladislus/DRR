#include "Test.hpp"

#include <iostream>

Test::Test( const char* name, uint32_t priority ): _name { name }, _priority { priority }
{
    ::std::cout << "Test( \"" << this->_name << ", " << this->_priority << " )" << ::std::endl;
}

void Test::run()
{
    ::std::cout << "Running test..." << ::std::endl;
}