cmake_minimum_required(VERSION 3.22.1)
project(shorebird_e2e_fixture LANGUAGES CXX)
if(NOT ANDROID_ABI STREQUAL "__ANDROID_ABI__")
  message(FATAL_ERROR "The disposable lifecycle fixture requires __ANDROID_ABI__")
endif()
add_library(shorebird_e2e_fixture SHARED fixture_jni.cpp)
target_compile_features(shorebird_e2e_fixture PRIVATE cxx_std_17)
target_compile_options(shorebird_e2e_fixture PRIVATE -Wall -Wextra -Werror -fvisibility=hidden)
target_link_libraries(shorebird_e2e_fixture PRIVATE dl)
# The generator copies the canonical public engine header beside this source.
# Never build/link a second updater into this test fixture.
