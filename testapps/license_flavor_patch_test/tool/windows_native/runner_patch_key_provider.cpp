#include "runner_patch_key_provider.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
// Windows types must precede the extension headers.
// clang-format off
#include <windows.h>
#include <bcrypt.h>
#include <wincrypt.h>
#include <winhttp.h>
// clang-format on

// Use the engine's public ABI; do not duplicate the callback declaration.
#include <flutter_windows.h>

#include <array>
#include <cstring>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {
constexpr size_t kKeySize = 32;
constexpr size_t kMaxRequest = 32 * 1024;
constexpr DWORD kMaxBlob = 16 * 1024;
constexpr DWORD kTimeoutMs = 3000;

struct SecretKey {
  std::array<uint8_t, kKeySize> bytes{};
  ~SecretKey() { SecureZeroMemory(bytes.data(), bytes.size()); }
};
struct ResponseBytes {
  std::array<char, 65> bytes{};
  ~ResponseBytes() { SecureZeroMemory(bytes.data(), bytes.size()); }
};
struct LocalBlob {
  DATA_BLOB blob{};
  ~LocalBlob() {
    if (blob.pbData != nullptr) {
      SecureZeroMemory(blob.pbData, blob.cbData);
      LocalFree(blob.pbData);
    }
  }
};
class InternetHandle {
 public:
  explicit InternetHandle(HINTERNET handle) : handle_(handle) {}
  ~InternetHandle() {
    if (handle_ != nullptr) WinHttpCloseHandle(handle_);
  }
  InternetHandle(const InternetHandle&) = delete;
  InternetHandle& operator=(const InternetHandle&) = delete;
  HINTERNET get() const { return handle_; }

 private:
  HINTERNET handle_;
};
class FileHandle {
 public:
  explicit FileHandle(HANDLE handle) : handle_(handle) {}
  ~FileHandle() { Close(); }
  FileHandle(const FileHandle&) = delete;
  FileHandle& operator=(const FileHandle&) = delete;
  HANDLE get() const { return handle_; }
  void Close() {
    if (handle_ != INVALID_HANDLE_VALUE) {
      CloseHandle(handle_);
      handle_ = INVALID_HANDLE_VALUE;
    }
  }

 private:
  HANDLE handle_;
};
struct Config {
  std::wstring cache_dir;
  std::wstring mode;
  INTERNET_PORT port = 0;
  std::wstring token;
  ~Config() {
    if (!token.empty())
      SecureZeroMemory(token.data(), token.size() * sizeof(wchar_t));
  }
};
std::mutex g_mutex;
Config g_config;
bool g_registered = false;

// Distinguish missing/empty from an oversized or otherwise unreadable variable.
bool ReadEnvironment(const wchar_t* name, std::wstring* value) {
  SetLastError(ERROR_SUCCESS);
  DWORD length = GetEnvironmentVariableW(name, nullptr, 0);
  if (length == 0) {
    value->clear();
    return GetLastError() == ERROR_ENVVAR_NOT_FOUND ||
           GetLastError() == ERROR_SUCCESS;
  }
  if (length > 32768) return false;
  std::vector<wchar_t> buffer(length);
  DWORD copied = GetEnvironmentVariableW(name, buffer.data(), length);
  if (copied == 0 || copied >= length) {
    SecureZeroMemory(buffer.data(), buffer.size() * sizeof(wchar_t));
    return false;
  }
  value->assign(buffer.data(), copied);
  SecureZeroMemory(buffer.data(), buffer.size() * sizeof(wchar_t));
  return true;
}

bool IsAbsoluteCachePath(const std::wstring& path) {
  // Reject UNC/device/relative paths. The harness owns a local absolute
  // directory.
  return path.size() >= 3 &&
         ((path[0] >= L'A' && path[0] <= L'Z') ||
          (path[0] >= L'a' && path[0] <= L'z')) &&
         path[1] == L':' && path[2] == L'\\' &&
         path.find_first_of(L"/\r\n") == std::wstring::npos;
}

bool ParseProvisionUrl(const std::wstring& url, INTERNET_PORT* port) {
  const std::wstring prefix = L"http://127.0.0.1:";
  const std::wstring suffix = L"/key";
  if (url.size() <= prefix.size() + suffix.size() ||
      url.compare(0, prefix.size(), prefix) != 0 ||
      url.compare(url.size() - suffix.size(), suffix.size(), suffix) != 0)
    return false;
  const size_t end = url.size() - suffix.size();
  if (end - prefix.size() > 5 || url[prefix.size()] == L'0') return false;
  unsigned int number = 0;
  for (size_t i = prefix.size(); i < end; ++i) {
    if (url[i] < L'0' || url[i] > L'9') return false;
    number = number * 10 + static_cast<unsigned int>(url[i] - L'0');
  }
  if (number == 0 || number > 65535) return false;
  *port = static_cast<INTERNET_PORT>(number);
  return true;
}

bool ValidToken(const std::wstring& token) {
  if (token.empty() || token.size() > 512) return false;
  for (wchar_t c : token) {
    if (!((c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z') ||
          (c >= L'0' && c <= L'9') || c == L'-' || c == L'_' || c == L'.' ||
          c == L'~'))
      return false;
  }
  return true;
}

bool BoundedUtf8(const char* text, size_t* length) {
  if (text == nullptr) return false;
  size_t count = 0;
  while (count <= kMaxRequest && text[count] != '\0') ++count;
  if (count == 0 || count > kMaxRequest) return false;
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text,
                          static_cast<int>(count), nullptr, 0) == 0)
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

std::string Hex(const uint8_t* bytes, size_t length) {
  constexpr char hex[] = "0123456789abcdef";
  std::string result;
  result.reserve(length * 2);
  for (size_t i = 0; i < length; ++i) {
    result.push_back(hex[bytes[i] >> 4]);
    result.push_back(hex[bytes[i] & 15]);
  }
  return result;
}

bool ContextDigest(const char* key_id, size_t key_id_length, const char* aad,
                   size_t aad_length, std::array<uint8_t, kKeySize>* digest) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                  0) < 0)
    return false;
  // Windows 7+ allocates the hash object when pbHashObject is null.
  bool ok = BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0;
  if (ok)
    ok = BCryptHashData(hash,
                        reinterpret_cast<PUCHAR>(const_cast<char*>(key_id)),
                        static_cast<ULONG>(key_id_length + 1), 0) >= 0;
  if (ok)
    ok = BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char*>(aad)),
                        static_cast<ULONG>(aad_length), 0) >= 0;
  if (ok)
    ok = BCryptFinishHash(hash, digest->data(),
                          static_cast<ULONG>(digest->size()), 0) >= 0;
  if (hash != nullptr) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok;
}

std::wstring CachePath(const Config& config,
                       const std::array<uint8_t, kKeySize>& digest) {
  std::string name = Hex(digest.data(), digest.size());
  return config.cache_dir + L"\\" + std::wstring(name.begin(), name.end()) +
         L".dpapi";
}

bool ReadCachedKey(const std::wstring& path,
                   const std::array<uint8_t, kKeySize>& digest,
                   SecretKey* key) {
  FileHandle file(CreateFileW(path.c_str(), GENERIC_READ, 0, nullptr,
                              OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT,
                              nullptr));
  if (file.get() == INVALID_HANDLE_VALUE) return false;
  BY_HANDLE_FILE_INFORMATION info{};
  if (!GetFileInformationByHandle(file.get(), &info) ||
      (info.dwFileAttributes &
       (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != 0 ||
      info.nFileSizeHigh != 0 || info.nFileSizeLow == 0 ||
      info.nFileSizeLow > kMaxBlob)
    return false;
  std::vector<uint8_t> encrypted(info.nFileSizeLow);
  DWORD read = 0;
  if (!ReadFile(file.get(), encrypted.data(),
                static_cast<DWORD>(encrypted.size()), &read, nullptr) ||
      read != encrypted.size())
    return false;
  DATA_BLOB input{static_cast<DWORD>(encrypted.size()), encrypted.data()};
  DATA_BLOB entropy{static_cast<DWORD>(digest.size()),
                    const_cast<BYTE*>(digest.data())};
  LocalBlob plaintext;
  if (!CryptUnprotectData(&input, nullptr, &entropy, nullptr, nullptr,
                          CRYPTPROTECT_UI_FORBIDDEN, &plaintext.blob) ||
      plaintext.blob.cbData != kKeySize)
    return false;
  std::memcpy(key->bytes.data(), plaintext.blob.pbData, kKeySize);
  return true;
}

bool WriteCachedKey(const std::wstring& path,
                    const std::array<uint8_t, kKeySize>& digest,
                    SecretKey* key) {
  DATA_BLOB input{static_cast<DWORD>(key->bytes.size()), key->bytes.data()};
  DATA_BLOB entropy{static_cast<DWORD>(digest.size()),
                    const_cast<BYTE*>(digest.data())};
  LocalBlob encrypted;
  if (!CryptProtectData(&input, L"Shorebird native E2E derived key", &entropy,
                        nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN,
                        &encrypted.blob) ||
      encrypted.blob.cbData == 0 || encrypted.blob.cbData > kMaxBlob)
    return false;
  std::array<uint8_t, 16> random{};
  if (BCryptGenRandom(nullptr, random.data(), static_cast<ULONG>(random.size()),
                      BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0)
    return false;
  std::string suffix = Hex(random.data(), random.size());
  const std::wstring temporary =
      path + L"." + std::wstring(suffix.begin(), suffix.end()) + L".tmp";
  // CREATE_NEW, no sharing, and null SECURITY_ATTRIBUTES prevent writable
  // aliases through this handle or child-process inheritance. Only encrypted
  // bytes enter it.
  FileHandle file(
      CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                  FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, nullptr));
  if (file.get() == INVALID_HANDLE_VALUE) return false;
  DWORD written = 0;
  const bool flushed = WriteFile(file.get(), encrypted.blob.pbData,
                                 encrypted.blob.cbData, &written, nullptr) &&
                       written == encrypted.blob.cbData &&
                       FlushFileBuffers(file.get());
  file.Close();
  const bool renamed = flushed && MoveFileExW(temporary.c_str(), path.c_str(),
                                              MOVEFILE_REPLACE_EXISTING |
                                                  MOVEFILE_WRITE_THROUGH);
  if (!renamed) DeleteFileW(temporary.c_str());
  return renamed;
}

bool DecodeResponse(const char* response, size_t length, SecretKey* key) {
  if (length != kKeySize * 2) return false;
  for (size_t i = 0; i < length; ++i) {
    if (!((response[i] >= '0' && response[i] <= '9') ||
          (response[i] >= 'a' && response[i] <= 'f')))
      return false;
  }
  auto nibble = [](char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
  for (size_t i = 0; i < key->bytes.size(); ++i) {
    key->bytes[i] = static_cast<uint8_t>((nibble(response[i * 2]) << 4) |
                                         nibble(response[i * 2 + 1]));
  }
  return true;
}

bool ProvisionKey(const Config& config, const char* key_id,
                  size_t key_id_length, const char* aad, size_t aad_length,
                  SecretKey* key) {
  if (config.port == 0 || config.token.empty()) return false;
  std::string body = "{\"key_id\":";
  AppendJsonString(key_id, key_id_length, &body);
  body.append(",\"aad\":");
  AppendJsonString(aad, aad_length, &body);
  body.push_back('}');
  if (body.size() > kMaxRequest) return false;
  InternetHandle session(
      WinHttpOpen(L"Shorebird native E2E", WINHTTP_ACCESS_TYPE_NO_PROXY,
                  WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
  if (!session.get() || !WinHttpSetTimeouts(session.get(), kTimeoutMs,
                                            kTimeoutMs, kTimeoutMs, kTimeoutMs))
    return false;
  InternetHandle connection(
      WinHttpConnect(session.get(), L"127.0.0.1", config.port, 0));
  if (!connection.get()) return false;
  InternetHandle request(WinHttpOpenRequest(connection.get(), L"POST", L"/key",
                                            nullptr, WINHTTP_NO_REFERER,
                                            WINHTTP_DEFAULT_ACCEPT_TYPES, 0));
  if (!request.get()) return false;
  DWORD disabled = WINHTTP_DISABLE_REDIRECTS | WINHTTP_DISABLE_COOKIES |
                   WINHTTP_DISABLE_AUTHENTICATION;
  DWORD header_limit = kMaxBlob;
  if (!WinHttpSetOption(request.get(), WINHTTP_OPTION_DISABLE_FEATURE,
                        &disabled, sizeof(disabled)) ||
      !WinHttpSetOption(request.get(), WINHTTP_OPTION_MAX_RESPONSE_HEADER_SIZE,
                        &header_limit, sizeof(header_limit)))
    return false;
  std::wstring headers =
      L"Content-Type: application/json\r\nAuthorization: Bearer " +
      config.token + L"\r\n";
  const ULONGLONG deadline = GetTickCount64() + 10000;
  const bool sent =
      WinHttpSendRequest(request.get(), headers.c_str(),
                         static_cast<DWORD>(headers.size()), body.data(),
                         static_cast<DWORD>(body.size()),
                         static_cast<DWORD>(body.size()), 0) != FALSE;
  SecureZeroMemory(headers.data(), headers.size() * sizeof(wchar_t));
  if (!sent || !WinHttpReceiveResponse(request.get(), nullptr)) return false;
  DWORD status = 0;
  DWORD status_size = sizeof(status);
  if (!WinHttpQueryHeaders(
          request.get(), WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
          WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
          WINHTTP_NO_HEADER_INDEX) ||
      status != 200)
    return false;
  ResponseBytes response;
  size_t used = 0;
  while (used < response.bytes.size()) {
    if (GetTickCount64() >= deadline) return false;
    DWORD read = 0;
    if (!WinHttpReadData(request.get(), response.bytes.data() + used,
                         static_cast<DWORD>(response.bytes.size() - used),
                         &read))
      return false;
    if (read == 0) return DecodeResponse(response.bytes.data(), used, key);
    used += read;
  }
  return false;
}

bool LookupKey(const char* key_id, const char* aad, uint8_t* key_out,
               size_t capacity, size_t* written) {
  if (written != nullptr) *written = 0;
  if (written == nullptr || key_out == nullptr || capacity < kKeySize)
    return false;
  try {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_registered || g_config.mode == L"missing") return false;
    if (g_config.mode == L"wrong") {
      SecureZeroMemory(key_out, kKeySize);
      *written = kKeySize;
      return true;
    }
    size_t key_id_length = 0;
    size_t aad_length = 0;
    if (!BoundedUtf8(key_id, &key_id_length) || !BoundedUtf8(aad, &aad_length))
      return false;
    std::array<uint8_t, kKeySize> digest{};
    if (!ContextDigest(key_id, key_id_length, aad, aad_length, &digest))
      return false;
    const std::wstring path = CachePath(g_config, digest);
    SecretKey key;
    if (!ReadCachedKey(path, digest, &key) &&
        (!ProvisionKey(g_config, key_id, key_id_length, aad, aad_length,
                       &key) ||
         !WriteCachedKey(path, digest, &key)))
      return false;
    std::memcpy(key_out, key.bytes.data(), key.bytes.size());
    *written = key.bytes.size();
    return true;
  } catch (...) {
    // C ABI callbacks must fail closed rather than unwind through the updater.
    return false;
  }
}
}  // namespace

bool RegisterWindowsNativeCacheTestProvider() {
  try {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_registered) return false;
    Config config;
    std::wstring url;
    if (!ReadEnvironment(L"SHOREBIRD_E2E_KEY_CACHE_DIR", &config.cache_dir) ||
        !IsAbsoluteCachePath(config.cache_dir) ||
        !ReadEnvironment(L"SHOREBIRD_E2E_KEY_MODE", &config.mode) ||
        !ReadEnvironment(L"SHOREBIRD_E2E_PROVISION_URL", &url) ||
        !ReadEnvironment(L"SHOREBIRD_E2E_PROVISION_TOKEN", &config.token))
      return false;
    if (config.mode.empty()) config.mode = L"correct";
    if (config.mode != L"correct" && config.mode != L"missing" &&
        config.mode != L"wrong")
      return false;
    if (url.empty() != config.token.empty()) return false;
    if (!url.empty() &&
        (!ParseProvisionUrl(url, &config.port) || !ValidToken(config.token)))
      return false;
    g_config.cache_dir = std::move(config.cache_dir);
    g_config.mode = std::move(config.mode);
    g_config.port = config.port;
    g_config.token = std::move(config.token);
    // Registration itself does no cache I/O, including in missing/wrong modes.
    g_registered = true;
    if (!shorebird_set_patch_decryption_key_provider(LookupKey)) {
      g_registered = false;
      SecureZeroMemory(g_config.token.data(),
                       g_config.token.size() * sizeof(wchar_t));
      g_config.token.clear();
      return false;
    }
    return true;
  } catch (...) {
    return false;
  }
}
