#include "runner_patch_key_provider.h"

// Use the actual exported engine ABI, including its callback declaration.
#include <curl/curl.h>
#include <flutter_linux/flutter_linux.h>
#include <glib.h>
#include <keyutils.h>

#include <array>
#include <cerrno>
#include <climits>
#include <cstring>
#include <mutex>
#include <string>
#include <utility>

namespace {
constexpr size_t kKeySize = 32;
constexpr size_t kMaxRequest = 32 * 1024;
constexpr unsigned int kKeyLifetimeSeconds = 3600;
constexpr char kDescriptionPrefix[] = "shorebird-e2e-v1:";

void Erase(void* pointer, size_t size) {
  volatile unsigned char* bytes = static_cast<volatile unsigned char*>(pointer);
  while (size-- != 0) *bytes++ = 0;
}

void EraseString(std::string* value) {
  if (!value->empty()) Erase(&(*value)[0], value->size());
}

struct SecretKey {
  std::array<uint8_t, kKeySize> bytes{};
  ~SecretKey() { Erase(bytes.data(), bytes.size()); }
};

struct ResponseBytes {
  std::array<char, 65> bytes{};
  size_t used = 0;
  ~ResponseBytes() { Erase(bytes.data(), bytes.size()); }
};

struct Config {
  key_serial_t keyring = 0;
  std::string mode;
  std::string url;
  std::string token;
  ~Config() { EraseString(&token); }
};

std::mutex g_mutex;
Config g_config;
bool g_registered = false;
bool g_curl_initialized = false;

bool ReadEnvironment(const char* name, std::string* result) {
  const char* value = g_getenv(name);
  if (value == nullptr) {
    result->clear();
    return true;
  }
  const size_t length = strnlen(value, kMaxRequest + 1);
  if (length > kMaxRequest) return false;
  result->assign(value, length);
  return true;
}

bool PositiveDecimal(const std::string& value, unsigned int maximum,
                     unsigned int* result) {
  if (value.empty() || value.size() > 10 || value[0] == '0') return false;
  unsigned int number = 0;
  for (char c : value) {
    if (c < '0' || c > '9') return false;
    const unsigned int digit = static_cast<unsigned int>(c - '0');
    if (number > maximum / 10 ||
        (number == maximum / 10 && digit > maximum % 10))
      return false;
    number = number * 10 + digit;
  }
  *result = number;
  return number != 0;
}

bool ParseProvisionUrl(const std::string& url) {
  const std::string prefix = "http://127.0.0.1:";
  const std::string suffix = "/key";
  if (url.size() <= prefix.size() + suffix.size() ||
      url.compare(0, prefix.size(), prefix) != 0 ||
      url.compare(url.size() - suffix.size(), suffix.size(), suffix) != 0)
    return false;
  unsigned int port = 0;
  const std::string digits =
      url.substr(prefix.size(), url.size() - prefix.size() - suffix.size());
  return digits.size() <= 5 && PositiveDecimal(digits, 65535, &port);
}

bool ValidToken(const std::string& token) {
  if (token.empty() || token.size() > 512) return false;
  for (char c : token) {
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
          c == '~'))
      return false;
  }
  return true;
}

bool BoundedUtf8(const char* text, size_t* length) {
  if (text == nullptr) return false;
  const size_t count = strnlen(text, kMaxRequest + 1);
  if (count == 0 || count > kMaxRequest ||
      !g_utf8_validate(text, static_cast<gssize>(count), nullptr))
    return false;
  *length = count;
  return true;
}

void AppendJsonString(const char* text, size_t length, std::string* result) {
  constexpr char hex[] = "0123456789abcdef";
  result->push_back('"');
  for (size_t i = 0; i < length; ++i) {
    const unsigned char c = static_cast<unsigned char>(text[i]);
    if (c == '"' || c == '\\') {
      result->push_back('\\');
      result->push_back(static_cast<char>(c));
    } else if (c < 0x20) {
      result->append("\\u00");
      result->push_back(hex[c >> 4]);
      result->push_back(hex[c & 15]);
    } else {
      result->push_back(static_cast<char>(c));
    }
  }
  result->push_back('"');
}

std::string ContextDescription(const char* key_id, size_t key_id_length,
                               const char* aad, size_t aad_length) {
  GChecksum* hash = g_checksum_new(G_CHECKSUM_SHA256);
  if (hash == nullptr) return {};
  g_checksum_update(hash, reinterpret_cast<const guchar*>(key_id),
                    static_cast<gssize>(key_id_length + 1));
  g_checksum_update(hash, reinterpret_cast<const guchar*>(aad),
                    static_cast<gssize>(aad_length));
  std::string description = kDescriptionPrefix;
  description.append(g_checksum_get_string(hash));
  g_checksum_free(hash);
  return description;
}

// Verify the final permissions before ever reading a user-key payload. This
// provider never uses a user/persistent keyring or a plaintext disk key file.
bool RestrictedKey(key_serial_t serial, const std::string& description) {
  std::array<char, 512> metadata{};
  const long size = keyctl_describe(serial, metadata.data(), metadata.size());
  if (size <= 0 || static_cast<size_t>(size) > metadata.size()) return false;
  const char* fields[5] = {metadata.data(), nullptr, nullptr, nullptr, nullptr};
  for (size_t i = 1; i < 5; ++i) {
    char* delimiter = std::strchr(const_cast<char*>(fields[i - 1]), ';');
    if (delimiter == nullptr) return false;
    *delimiter = '\0';
    fields[i] = delimiter + 1;
  }
  // KEY_POS_ALL is 0x3f000000; all user/group/other permission bits are zero.
  return std::strcmp(fields[0], "user") == 0 &&
         std::strcmp(fields[3], "3f000000") == 0 && description == fields[4];
}

enum class CachedKey { kFound, kMissing, kInvalid };

CachedKey ReadCachedKey(key_serial_t keyring, const std::string& description,
                        SecretKey* key) {
  const key_serial_t serial =
      keyctl_search(keyring, "user", description.c_str(), 0);
  if (serial < 0)
    return errno == ENOKEY ? CachedKey::kMissing : CachedKey::kInvalid;
  if (!RestrictedKey(serial, description)) return CachedKey::kInvalid;
  const long size = keyctl_read(serial, key->bytes.data(), key->bytes.size());
  return size == static_cast<long>(kKeySize) ? CachedKey::kFound
                                             : CachedKey::kInvalid;
}

bool WriteCachedKey(key_serial_t keyring, const std::string& description,
                    const SecretKey& key) {
  const key_serial_t serial = add_key(
      "user", description.c_str(), key.bytes.data(), key.bytes.size(), keyring);
  if (serial < 0) return false;
  if (keyctl_setperm(serial, KEY_POS_ALL) < 0 ||
      keyctl_set_timeout(serial, kKeyLifetimeSeconds) < 0 ||
      !RestrictedKey(serial, description)) {
    keyctl_revoke(serial);
    keyctl_unlink(serial, keyring);
    return false;
  }
  return true;
}

bool DecodeResponse(const char* response, size_t length, SecretKey* key) {
  if (length != kKeySize * 2) return false;
  for (size_t i = 0; i < length; ++i) {
    if (!((response[i] >= '0' && response[i] <= '9') ||
          (response[i] >= 'a' && response[i] <= 'f')))
      return false;
  }
  const auto nibble = [](char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
  for (size_t i = 0; i < key->bytes.size(); ++i)
    key->bytes[i] = static_cast<uint8_t>((nibble(response[i * 2]) << 4) |
                                         nibble(response[i * 2 + 1]));
  return true;
}

size_t ReceiveResponse(char* data, size_t size, size_t count, void* context) {
  auto* response = static_cast<ResponseBytes*>(context);
  if (size != 0 && count > response->bytes.size() / size) return 0;
  const size_t bytes = size * count;
  if (bytes > response->bytes.size() - response->used) return 0;
  std::memcpy(response->bytes.data() + response->used, data, bytes);
  response->used += bytes;
  return bytes;
}

struct CurlRequest {
  CURL* handle = nullptr;
  curl_slist* headers = nullptr;
  ~CurlRequest() {
    if (handle != nullptr) curl_easy_cleanup(handle);
    for (curl_slist* header = headers; header != nullptr; header = header->next)
      Erase(header->data, std::strlen(header->data));
    curl_slist_free_all(headers);
  }
  bool AddHeader(const char* value) {
    curl_slist* updated = curl_slist_append(headers, value);
    if (updated == nullptr) return false;
    headers = updated;
    return true;
  }
};

struct SecretText {
  std::string value;
  ~SecretText() { EraseString(&value); }
};

bool ProvisionKey(const Config& config, const char* key_id,
                  size_t key_id_length, const char* aad, size_t aad_length,
                  SecretKey* key) {
  if (config.url.empty() || config.token.empty()) return false;
  std::string body = "{\"key_id\":";
  AppendJsonString(key_id, key_id_length, &body);
  body.append(",\"aad\":");
  AppendJsonString(aad, aad_length, &body);
  body.push_back('}');
  if (body.size() > kMaxRequest) return false;
  if (!g_curl_initialized) {
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return false;
    // The callback and curl global state intentionally live until process exit.
    g_curl_initialized = true;
  }
  CurlRequest request;
  request.handle = curl_easy_init();
  SecretText authorization;
  authorization.value = "Authorization: Bearer ";
  authorization.value.append(config.token);
  if (request.handle == nullptr ||
      !request.AddHeader("Content-Type: application/json") ||
      !request.AddHeader(authorization.value.c_str()))
    return false;
  ResponseBytes response;
  // Exact IPv4 literal URL + no proxy/redirect/cookie/auth negotiation. No
  // hostname resolution, remote destination, or ambient proxy configuration.
#define SET_OPTION(option, value) \
  if (curl_easy_setopt(request.handle, option, value) != CURLE_OK) return false
  SET_OPTION(CURLOPT_URL, config.url.c_str());
  SET_OPTION(CURLOPT_PROXY, "");
  SET_OPTION(CURLOPT_NOPROXY, "*");
#if LIBCURL_VERSION_NUM >= 0x075500
  SET_OPTION(CURLOPT_PROTOCOLS_STR, "http");
  SET_OPTION(CURLOPT_REDIR_PROTOCOLS_STR, "http");
#else
  SET_OPTION(CURLOPT_PROTOCOLS, static_cast<long>(CURLPROTO_HTTP));
  SET_OPTION(CURLOPT_REDIR_PROTOCOLS, static_cast<long>(CURLPROTO_HTTP));
#endif
  SET_OPTION(CURLOPT_FOLLOWLOCATION, 0L);
  SET_OPTION(CURLOPT_MAXREDIRS, 0L);
  SET_OPTION(CURLOPT_NETRC, static_cast<long>(CURL_NETRC_IGNORED));
  SET_OPTION(CURLOPT_HTTPAUTH, static_cast<long>(CURLAUTH_NONE));
  SET_OPTION(CURLOPT_HTTP_VERSION, static_cast<long>(CURL_HTTP_VERSION_1_1));
  SET_OPTION(CURLOPT_NOSIGNAL, 1L);
  SET_OPTION(CURLOPT_CONNECTTIMEOUT_MS, 3000L);
  SET_OPTION(CURLOPT_TIMEOUT_MS, 10000L);
  SET_OPTION(CURLOPT_HTTPHEADER, request.headers);
  SET_OPTION(CURLOPT_POST, 1L);
  SET_OPTION(CURLOPT_POSTFIELDS, body.data());
  SET_OPTION(CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
  SET_OPTION(CURLOPT_WRITEFUNCTION, ReceiveResponse);
  SET_OPTION(CURLOPT_WRITEDATA, &response);
#undef SET_OPTION
  if (curl_easy_perform(request.handle) != CURLE_OK) return false;
  long status = 0;
  return curl_easy_getinfo(request.handle, CURLINFO_RESPONSE_CODE, &status) ==
             CURLE_OK &&
         status == 200 &&
         DecodeResponse(response.bytes.data(), response.used, key);
}

bool LookupKey(const char* key_id, const char* aad, uint8_t* key_out,
               size_t capacity, size_t* written) {
  if (written != nullptr) *written = 0;
  if (written == nullptr || key_out == nullptr || capacity < kKeySize)
    return false;
  try {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_registered) return false;
    size_t key_id_length = 0;
    size_t aad_length = 0;
    if (!BoundedUtf8(key_id, &key_id_length) || !BoundedUtf8(aad, &aad_length))
      return false;
    // Failure modes deliberately perform no keyring read/write or HTTP I/O.
    if (g_config.mode == "missing") return false;
    if (g_config.mode == "wrong") {
      Erase(key_out, kKeySize);
      *written = kKeySize;
      return true;
    }
    if (keyctl_get_keyring_ID(KEY_SPEC_SESSION_KEYRING, 0) != g_config.keyring)
      return false;
    const std::string description =
        ContextDescription(key_id, key_id_length, aad, aad_length);
    if (description.empty()) return false;
    SecretKey key;
    const CachedKey cached = ReadCachedKey(g_config.keyring, description, &key);
    if (cached == CachedKey::kInvalid ||
        (cached == CachedKey::kMissing &&
         (!ProvisionKey(g_config, key_id, key_id_length, aad, aad_length,
                        &key) ||
          !WriteCachedKey(g_config.keyring, description, key))))
      return false;
    std::memcpy(key_out, key.bytes.data(), key.bytes.size());
    *written = key.bytes.size();
    return true;
  } catch (...) {
    // Never unwind a C ABI callback through the updater.
    return false;
  }
}
}  // namespace

bool RegisterLinuxNativeCacheTestProvider() {
  try {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_registered) return false;
    Config config;
    std::string keyring;
    unsigned int serial = 0;
    if (!ReadEnvironment("SHOREBIRD_E2E_KEYRING_ID", &keyring) ||
        !PositiveDecimal(keyring, INT_MAX, &serial) ||
        !ReadEnvironment("SHOREBIRD_E2E_KEY_MODE", &config.mode) ||
        !ReadEnvironment("SHOREBIRD_E2E_PROVISION_URL", &config.url) ||
        !ReadEnvironment("SHOREBIRD_E2E_PROVISION_TOKEN", &config.token))
      return false;
    config.keyring = static_cast<key_serial_t>(serial);
    if (keyctl_get_keyring_ID(KEY_SPEC_SESSION_KEYRING, 0) != config.keyring)
      return false;
    if (config.mode.empty()) config.mode = "correct";
    if (config.mode != "correct" && config.mode != "missing" &&
        config.mode != "wrong")
      return false;
    if (config.url.empty() != config.token.empty() ||
        (!config.url.empty() &&
         (!ParseProvisionUrl(config.url) || !ValidToken(config.token))))
      return false;
    g_config.keyring = config.keyring;
    g_config.mode = std::move(config.mode);
    g_config.url = std::move(config.url);
    g_config.token = config.token;
    g_registered = true;
    if (!shorebird_set_patch_decryption_key_provider(LookupKey)) {
      g_registered = false;
      EraseString(&g_config.token);
      g_config.token.clear();
      return false;
    }
    return true;
  } catch (...) {
    return false;
  }
}
