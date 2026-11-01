#ifndef RUNNER_LINUX_PATCH_KEY_PROVIDER_H_
#define RUNNER_LINUX_PATCH_KEY_PROVIDER_H_

// Test-only. Call once before constructing the GTK application/Flutter engine.
// False means invalid fixture configuration or public API registration failed.
bool RegisterLinuxNativeCacheTestProvider();

#endif  // RUNNER_LINUX_PATCH_KEY_PROVIDER_H_
