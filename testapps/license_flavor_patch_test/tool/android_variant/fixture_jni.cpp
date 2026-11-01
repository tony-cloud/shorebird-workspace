#include "shorebird_patch_encryption.h"
#include <jni.h>
#include <dlfcn.h>
#include <array>
#include <cstring>
#include <cstdlib>
#include <mutex>
#include <string>

// Test-only bridge to the public native V2 API. Never links another updater.
namespace {
JavaVM* g_vm = nullptr;
jobject g_provider = nullptr;
jmethodID g_resolve = nullptr;
std::mutex g_config_mutex;
bool g_configured = false;
void Erase(void* p, size_t n) {
  volatile unsigned char* b = static_cast<volatile unsigned char*>(p);
  while (n-- != 0) *b++ = 0;
}
struct Key {
  std::array<uint8_t, 32> bytes{};
  ~Key() { Erase(bytes.data(), bytes.size()); }
};
struct Env {
  JNIEnv* value = nullptr;
  bool owned = false;
  Env() {
    if (g_vm == nullptr) return;
    jint result = g_vm->GetEnv(reinterpret_cast<void**>(&value), JNI_VERSION_1_6);
    if (result == JNI_EDETACHED) {
      if (g_vm->AttachCurrentThread(&value, nullptr) == JNI_OK) owned = true;
      else value = nullptr;
    } else if (result != JNI_OK) value = nullptr;
  }
  ~Env() { if (owned) g_vm->DetachCurrentThread(); }
};
bool ClearException(JNIEnv* env) {
  if (!env->ExceptionCheck()) return false;
  env->ExceptionClear(); return true;
}
bool Bytes(JNIEnv* env, jbyteArray input, std::string* out, size_t limit = 262144) {
  if (input == nullptr) return false;
  jsize n = env->GetArrayLength(input);
  if (n <= 0 || static_cast<size_t>(n) > limit) return false;
  out->resize(n);
  env->GetByteArrayRegion(input, 0, n, reinterpret_cast<jbyte*>(&(*out)[0]));
  return !ClearException(env) && out->find('\0') == std::string::npos;
}
bool LookupKey(const char* id, const char* aad, const char* context,
               uint8_t* out, size_t capacity, size_t* written) {
  if (written != nullptr) *written = 0;
  if (out == nullptr || capacity < 32) return false;
  Erase(out, 32);
  if (written == nullptr || id == nullptr || aad == nullptr || context == nullptr ||
      g_provider == nullptr) return false;
  const std::array<const char*, 3> values{id, aad, context};
  const std::array<size_t, 3> limits{128, 8192, 65536};
  std::array<size_t, 3> sizes{};
  for (size_t i = 0; i < values.size(); i++) {
    sizes[i] = strnlen(values[i], limits[i] + 1);
    if (sizes[i] == 0 || sizes[i] > limits[i]) return false;
  }
  Env attached; JNIEnv* env = attached.value;
  if (env == nullptr) return false;
  if (env->PushLocalFrame(8) != JNI_OK) { ClearException(env); return false; }
  std::array<jbyteArray, 3> arrays{};
  bool ok = true;
  for (size_t i = 0; i < values.size(); i++) {
    arrays[i] = env->NewByteArray(static_cast<jsize>(sizes[i]));
    if (arrays[i] == nullptr || ClearException(env)) { ok = false; break; }
    env->SetByteArrayRegion(arrays[i], 0, static_cast<jsize>(sizes[i]),
                           reinterpret_cast<const jbyte*>(values[i]));
    if (ClearException(env)) { ok = false; break; }
  }
  Key local;
  jbyteArray key = nullptr;
  if (ok) {
    key = static_cast<jbyteArray>(env->CallObjectMethod(
        g_provider, g_resolve, arrays[0], arrays[1], arrays[2]));
    ok = !ClearException(env) && key != nullptr && env->GetArrayLength(key) == 32;
  }
  if (ok) {
    env->GetByteArrayRegion(key, 0, 32, reinterpret_cast<jbyte*>(local.bytes.data()));
    ok = !ClearException(env);
  }
  if (key != nullptr && !env->ExceptionCheck()) {
    jsize size = env->GetArrayLength(key);
    std::array<jbyte, 256> zeros{};
    for (jsize offset = 0; offset < size; offset += static_cast<jsize>(zeros.size())) {
      jsize count = size - offset;
      if (count > static_cast<jsize>(zeros.size())) count = static_cast<jsize>(zeros.size());
      env->SetByteArrayRegion(key, offset, count, zeros.data());
      if (ClearException(env)) { ok = false; break; }
    }
  }
  env->PopLocalFrame(nullptr);
  if (ClearException(env)) ok = false;
  if (ok) { std::memcpy(out, local.bytes.data(), 32); *written = 32; }
  return ok;
}

jboolean Configure(JNIEnv* env, jclass, jobject provider, jbyteArray nonce,
                   jbyteArray observer, jbyteArray token, jbyteArray app_data,
                   jbyteArray contexts, jbyteArray provider_receipt, jboolean check) {
  std::lock_guard<std::mutex> lock(g_config_mutex);
  if (g_configured || provider == nullptr) return JNI_FALSE;
  g_configured = true;
  std::array<jbyteArray, 6> inputs{nonce, observer, token, app_data, contexts, provider_receipt};
  std::array<std::string, 6> values;
  for (size_t i = 0; i < inputs.size(); i++) {
    if (!Bytes(env, inputs[i], &values[i])) return JNI_FALSE;
  }
  jclass provider_class = env->GetObjectClass(provider);
  if (provider_class == nullptr || ClearException(env)) return JNI_FALSE;
  g_resolve = env->GetMethodID(provider_class, "resolve", "([B[B[B)[B");
  if (ClearException(env) || g_resolve == nullptr) return JNI_FALSE;
  g_provider = env->NewGlobalRef(provider);
  env->DeleteLocalRef(provider_class);
  if (ClearException(env) || g_provider == nullptr) return JNI_FALSE;
  void* flutter = dlopen("libflutter.so", RTLD_NOW | RTLD_NOLOAD);
  if (flutter == nullptr) return JNI_FALSE;
  using Register = decltype(&shorebird_set_patch_decryption_key_provider_v2);
  auto registration = reinterpret_cast<Register>(
      dlsym(flutter, "shorebird_set_patch_decryption_key_provider_v2"));
  const std::array<const char*, 6> names{
      "SHOREBIRD_E2E_RECEIPT", "SHOREBIRD_E2E_OBSERVER", "SHOREBIRD_E2E_PROCESS_TOKEN",
      "SHOREBIRD_VARIANT_APP_DATA_DIR", "SHOREBIRD_VARIANT_CONTEXTS",
      "SHOREBIRD_VARIANT_PROVIDER_RECEIPT"};
  if (registration == nullptr) return JNI_FALSE;
  for (size_t i = 0; i < names.size(); i++) {
    if (setenv(names[i], values[i].c_str(), 1) != 0) return JNI_FALSE;
  }
  if (setenv("SHOREBIRD_VARIANT_CHECK", check ? "1" : "0", 1) != 0) return JNI_FALSE;
  return registration(LookupKey) ? JNI_TRUE : JNI_FALSE;
}
}  // namespace

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, void*) {
  g_vm = vm;
  JNIEnv* env = nullptr;
  if (vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) != JNI_OK) return JNI_ERR;
  std::string package = "__JAVA_PACKAGE__";
  for (char& c : package) if (c == '.') c = '/';
  jclass control = env->FindClass((package + "/ControlActivity").c_str());
  if (control == nullptr || ClearException(env)) return JNI_ERR;
  std::string signature = "(L" + package + "/PatchKeyProvider;[B[B[B[B[B[BZ)Z";
  const JNINativeMethod method{
      const_cast<char*>("nativeConfigure"), const_cast<char*>(signature.c_str()),
      reinterpret_cast<void*>(Configure)};
  jint registered = env->RegisterNatives(control, &method, 1);
  env->DeleteLocalRef(control);
  return registered == JNI_OK && !ClearException(env) ? JNI_VERSION_1_6 : JNI_ERR;
}
