// Opt-in v2 adapter around the unchanged app-owned session-keyring provider.
#include "native_variant_api.h"
#include <flutter_linux/flutter_linux.h>
#include <unistd.h>
#include "native_variant_context.h"
static bool RegisterVariantProvider(FlutterShorebirdPatchDecryptionKeyProvider);
#define shorebird_set_patch_decryption_key_provider RegisterVariantProvider
#include "legacy_provider.cpp"
#undef shorebird_set_patch_decryption_key_provider
static bool LookupVariantKey(const char* id, const char* aad, const char* context,
                             uint8_t* out, size_t capacity, size_t* written) {
  if (written) *written = 0;
  try {
    // Authorization is checked before LookupKey can touch even cached keys.
    const auto grant = native_variant::validate(id,aad,context);
    if (!LookupKey(id,aad,out,capacity,written)) return false;
    if (native_variant::receipt(grant,id,static_cast<unsigned long>(getpid()))) return true;
  } catch (...) {}
  if (out && capacity >= 32) Erase(out,32);
  if (written) *written = 0;
  return false;
}
static bool RegisterVariantProvider(FlutterShorebirdPatchDecryptionKeyProvider) {
  return shorebird_set_patch_decryption_key_provider_v2(LookupVariantKey);
}
