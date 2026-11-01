#include "shorebird_patch_encryption.h"
#include <jni.h>
#include <dlfcn.h>
#include <array>
#include <cstring>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>

namespace {
constexpr size_t kMaxText = 32768;
JavaVM* g_vm = nullptr;
jobject g_provider = nullptr;
jclass g_provider_class = nullptr;
jmethodID g_resolve = nullptr;
jmethodID g_hmac = nullptr;
std::mutex g_config_mutex;
bool g_configured = false;
thread_local bool g_callback_owned_attachment = false;

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
bool Bytes(JNIEnv* env, jbyteArray input, std::string* out) {
  if (input == nullptr) return false;
  jsize n = env->GetArrayLength(input);
  if (n <= 0 || n > static_cast<jsize>(kMaxText)) return false;
  out->resize(n);
  env->GetByteArrayRegion(input, 0, n, reinterpret_cast<jbyte*>(&(*out)[0]));
  return !ClearException(env) && out->find('\0') == std::string::npos;
}
bool LookupKey(const char* id, const char* aad, uint8_t* out,
               size_t capacity, size_t* written) {
  if (written != nullptr) *written = 0;
  g_callback_owned_attachment = false;
  if (written == nullptr || out == nullptr || capacity < 32 || id == nullptr ||
      aad == nullptr || g_provider == nullptr) return false;
  size_t id_size = strnlen(id, kMaxText + 1), aad_size = strnlen(aad, kMaxText + 1);
  if (id_size == 0 || id_size > kMaxText || aad_size == 0 || aad_size > kMaxText) return false;
  Env attached; JNIEnv* env = attached.value;
  if (env == nullptr) return false;
  g_callback_owned_attachment = attached.owned;
  if (env->PushLocalFrame(8) != JNI_OK) { ClearException(env); return false; }
  jbyteArray id_array = env->NewByteArray(static_cast<jsize>(id_size));
  jbyteArray aad_array = env->NewByteArray(static_cast<jsize>(aad_size));
  bool ok = id_array != nullptr && aad_array != nullptr && !ClearException(env);
  if (ok) {
    env->SetByteArrayRegion(id_array, 0, static_cast<jsize>(id_size), reinterpret_cast<const jbyte*>(id));
    env->SetByteArrayRegion(aad_array, 0, static_cast<jsize>(aad_size), reinterpret_cast<const jbyte*>(aad));
    ok = !ClearException(env);
  }
  jbyteArray key = nullptr;
  Key local;
  if (ok) {
    key = static_cast<jbyteArray>(env->CallObjectMethod(g_provider, g_resolve, id_array, aad_array));
    ok = !ClearException(env) && key != nullptr && env->GetArrayLength(key) == 32;
  }
  if (ok) {
    env->GetByteArrayRegion(key, 0, 32, reinterpret_cast<jbyte*>(local.bytes.data()));
    ok = !ClearException(env);
  }
  // Zero the Java return buffer even on a malformed length or JNI failure.
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
  if (ok) { std::memcpy(out, local.bytes.data(), 32); *written = 32; }
  env->PopLocalFrame(nullptr);
  if (ClearException(env)) { *written = 0; Erase(out, 32); return false; }
  return ok;
}

jboolean Configure(JNIEnv* env, jclass, jobject provider,
                   jbyteArray receipt, jbyteArray observer, jbyteArray token) {
  std::lock_guard<std::mutex> lock(g_config_mutex);
  if (g_configured || provider == nullptr) return JNI_FALSE;
  g_configured = true;  // Failed configuration cannot be retried in this process.
  std::string receipt_text, observer_text, token_text;
  if (!Bytes(env, receipt, &receipt_text) || !Bytes(env, observer, &observer_text) ||
      !Bytes(env, token, &token_text)) return JNI_FALSE;
  jclass provider_class = env->GetObjectClass(provider);
  if (provider_class == nullptr || ClearException(env)) return JNI_FALSE;
  g_resolve = env->GetMethodID(provider_class, "resolve", "([B[B)[B");
  g_hmac = env->GetStaticMethodID(provider_class, "hmac", "([B[B)[B");
  if (ClearException(env) || g_resolve == nullptr || g_hmac == nullptr) return JNI_FALSE;
  g_provider = env->NewGlobalRef(provider);
  g_provider_class = static_cast<jclass>(env->NewGlobalRef(provider_class));
  env->DeleteLocalRef(provider_class);
  if (ClearException(env) || g_provider == nullptr || g_provider_class == nullptr) return JNI_FALSE;
  // libflutter is already resident; RTLD_NOLOAD prevents a fixture-owned engine.
  void* flutter = dlopen("libflutter.so", RTLD_NOW | RTLD_NOLOAD);
  if (flutter == nullptr) return JNI_FALSE;
  using Register = decltype(&shorebird_set_patch_decryption_key_provider);
  auto registration = reinterpret_cast<Register>(dlsym(flutter, "shorebird_set_patch_decryption_key_provider"));
  bool ok = registration != nullptr &&
      setenv("SHOREBIRD_E2E_RECEIPT", receipt_text.c_str(), 1) == 0 &&
      setenv("SHOREBIRD_E2E_OBSERVER", observer_text.c_str(), 1) == 0 &&
      setenv("SHOREBIRD_E2E_PROCESS_TOKEN", token_text.c_str(), 1) == 0 && registration(LookupKey);
  // Keep the resident library's reference for the process-lifetime callback.
  return ok ? JNI_TRUE : JNI_FALSE;
}

jbyteArray Probe(JNIEnv* env, jclass, jbyteArray id, jbyteArray aad, jbyteArray challenge) {
  std::string id_text, aad_text;
  if (!Bytes(env, id, &id_text) || !Bytes(env, aad, &aad_text) ||
      challenge == nullptr || env->GetArrayLength(challenge) != 32 || g_provider == nullptr) return nullptr;
  Key challenge_bytes, mac;
  env->GetByteArrayRegion(challenge, 0, 32, reinterpret_cast<jbyte*>(challenge_bytes.bytes.data()));
  if (ClearException(env)) return nullptr;
  bool success = false;
  try {
    std::thread worker([&] {
      // The very same function pointer passed to the public engine API runs
      // from a genuinely new native thread (not an executor Java thread).
      JNIEnv* prior = nullptr;
      if (g_vm->GetEnv(reinterpret_cast<void**>(&prior), JNI_VERSION_1_6) != JNI_EDETACHED) return;
      Key key; size_t written = 0;
      if (!LookupKey(id_text.c_str(), aad_text.c_str(), key.bytes.data(), 32, &written) ||
          written != 32 || !g_callback_owned_attachment ||
          g_vm->GetEnv(reinterpret_cast<void**>(&prior), JNI_VERSION_1_6) != JNI_EDETACHED) return;
      Env attached; JNIEnv* e = attached.value;
      if (e == nullptr || e->PushLocalFrame(4) != JNI_OK) { if(e) ClearException(e); return; }
      jbyteArray key_array=e->NewByteArray(32), challenge_array=e->NewByteArray(32);
      bool ok = key_array != nullptr && challenge_array != nullptr && !ClearException(e);
      jbyteArray result=nullptr;
      if (ok) {
        e->SetByteArrayRegion(key_array,0,32,reinterpret_cast<const jbyte*>(key.bytes.data()));
        e->SetByteArrayRegion(challenge_array,0,32,reinterpret_cast<const jbyte*>(challenge_bytes.bytes.data()));
        ok = !ClearException(e);
      }
      if (ok) {
        result=static_cast<jbyteArray>(e->CallStaticObjectMethod(g_provider_class,g_hmac,key_array,challenge_array));
        ok=!ClearException(e) && result != nullptr && e->GetArrayLength(result)==32;
      }
      if (ok) { e->GetByteArrayRegion(result,0,32,reinterpret_cast<jbyte*>(mac.bytes.data())); ok=!ClearException(e); }
      std::array<jbyte,32> zero{};
      if (key_array) e->SetByteArrayRegion(key_array,0,32,zero.data());
      if (result) e->SetByteArrayRegion(result,0,32,zero.data());
      if (ClearException(e)) ok=false;
      e->PopLocalFrame(nullptr); if (ClearException(e)) ok=false;
      success=ok;
    });
    worker.join();
  } catch (...) { return nullptr; }
  if (!success) return nullptr;
  jbyteArray result=env->NewByteArray(32);
  if(result != nullptr) env->SetByteArrayRegion(result,0,32,reinterpret_cast<const jbyte*>(mac.bytes.data()));
  if(ClearException(env)) return nullptr;
  return result;
}
}  // namespace

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, void*) {
  g_vm=vm;
  JNIEnv* env=nullptr;
  if(vm->GetEnv(reinterpret_cast<void**>(&env),JNI_VERSION_1_6)!=JNI_OK) return JNI_ERR;
  std::string name="__JAVA_PACKAGE__";
  for(char& c:name) if(c=='.') c='/';
  name += "/ControlActivity";
  jclass control=env->FindClass(name.c_str());
  if(control==nullptr || ClearException(env)) return JNI_ERR;
  std::string configure_signature="(L";
  std::string provider_name="__JAVA_PACKAGE__";
  for(char& c:provider_name) if(c=='.') c='/';
  configure_signature+=provider_name+"/PatchKeyProvider;[B[B[B)Z";
  const JNINativeMethod methods[]={
      {const_cast<char*>("nativeConfigure"),const_cast<char*>(configure_signature.c_str()),reinterpret_cast<void*>(Configure)},
      {const_cast<char*>("nativeProbe"),const_cast<char*>("([B[B[B)[B"),reinterpret_cast<void*>(Probe)}};
  jint registered=env->RegisterNatives(control,methods,2);
  env->DeleteLocalRef(control);
  return registered==JNI_OK && !ClearException(env) ? JNI_VERSION_1_6 : JNI_ERR;
}
