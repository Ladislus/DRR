CMAKE_MINIMUM_REQUIRED( VERSION 3.23 )

PROJECT(
    "DRR"
    DESCRIPTION "Doxygen Requirement Reporter"
    LANGUAGES CXX
)

ADD_EXECUTABLE(
    Test
    "${CMAKE_CURRENT_SOURCE_DIR}/sources/Test.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/sources/main.cpp"
)
TARGET_INCLUDE_DIRECTORIES( Test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/includes" )
TARGET_COMPILE_OPTIONS( Test PRIVATE -std=gnu++20 -O3 -ggdb3 -Wall -Wextra -Werror )

FIND_PACKAGE( Doxygen REQUIRED )
FIND_PACKAGE( Python3 REQUIRED COMPONENTS Interpreter )

MAKE_DIRECTORY( "${CMAKE_CURRENT_BINARY_DIR}/generated" )

ADD_CUSTOM_COMMAND(
    TARGET Test POST_BUILD
    COMMAND
        $<TARGET_FILE:Python3::Interpreter> "${CMAKE_CURRENT_SOURCE_DIR}/scripts/requirement_generator.py"
            "${CMAKE_CURRENT_BINARY_DIR}/generated/requirement.hpp"
    COMMAND Doxygen::doxygen "${CMAKE_CURRENT_SOURCE_DIR}/Doxyfile"
    COMMAND
        $<TARGET_FILE:Python3::Interpreter> "${CMAKE_CURRENT_SOURCE_DIR}/scripts/djd_generator_v5.py"
            --xml-dir     "${CMAKE_CURRENT_BINARY_DIR}/Documentation/xml"
            --output-md   "${CMAKE_CURRENT_BINARY_DIR}/generated/report.md"
            --output-xlsx "${CMAKE_CURRENT_BINARY_DIR}/generated/report.xlsx"
    COMMENT "Generating Doxygen & DJD..."
)