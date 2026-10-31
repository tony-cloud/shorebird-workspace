// Standalone Windows test; intentionally includes the implementation so helpers
// stay private in the Runner. Do not also compile
// runner_patch_key_provider.cpp.
#include <cstdio>
#include <stdexcept>

#include "runner_patch_key_provider.cpp"

namespace {
FlutterShorebirdPatchDecryptionKeyProvider g_test_provider = nullptr;
constexpr char kTestId[] = "fixture-key";
constexpr char kTestAad[] = "app=temporary\nclient=\xc3\xa9";

void Check(bool value, const char* label) {
  if (!value) throw std::runtime_error(label);
}

void ClearEnvironment(const wchar_t* name) {
  SetLastError(ERROR_SUCCESS);
  Check(SetEnvironmentVariableW(name, nullptr) != FALSE ||
            GetLastError() == ERROR_ENVVAR_NOT_FOUND,
        "clear provisioning environment");
}

void Configure(const std::wstring& directory, const wchar_t* mode) {
  Check(SetEnvironmentVariableW(L"SHOREBIRD_E2E_KEY_CACHE_DIR",
                                directory.c_str()) != FALSE,
        "set cache environment");
  Check(SetEnvironmentVariableW(L"SHOREBIRD_E2E_KEY_MODE", mode) != FALSE,
        "set mode environment");
  ClearEnvironment(L"SHOREBIRD_E2E_PROVISION_URL");
  ClearEnvironment(L"SHOREBIRD_E2E_PROVISION_TOKEN");
}

void ExpectKey() {
  SecretKey output;
  size_t written = 99;
  Check(g_test_provider(kTestId, kTestAad, output.bytes.data(),
                        output.bytes.size(), &written),
        "offline key lookup");
  Check(written == kKeySize, "offline key length");
  for (size_t i = 0; i < output.bytes.size(); ++i)
    Check(output.bytes[i] == static_cast<uint8_t>(i + 1), "offline key bytes");
}

void RunColdRead(const std::wstring& directory) {
  std::array<wchar_t, 32768> executable{};
  DWORD length = GetModuleFileNameW(nullptr, executable.data(),
                                    static_cast<DWORD>(executable.size()));
  Check(length != 0 && length < executable.size(), "test executable path");
  std::wstring command = L"\"" + std::wstring(executable.data(), length) +
                         L"\" --cold-read \"" + directory + L"\"";
  STARTUPINFOW startup{};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process{};
  Check(CreateProcessW(executable.data(), command.data(), nullptr, nullptr,
                       FALSE, 0, nullptr, nullptr, &startup, &process) != FALSE,
        "start cold reader");
  FileHandle child(process.hProcess);
  FileHandle thread(process.hThread);
  DWORD wait = WaitForSingleObject(child.get(), 15000);
  if (wait != WAIT_OBJECT_0) {
    TerminateProcess(child.get(), 2);
    WaitForSingleObject(child.get(), 5000);
    Check(false, "cold reader timeout");
  }
  DWORD code = 1;
  Check(GetExitCodeProcess(child.get(), &code) && code == 0,
        "cold reader result");
}

struct TestDirectory {
  std::wstring path;
  std::wstring cache;
  ~TestDirectory() {
    if (!cache.empty()) DeleteFileW(cache.c_str());
    if (!path.empty()) RemoveDirectoryW(path.c_str());
  }
};

void RunTests() {
  INTERNET_PORT port = 0;
  Check(
      ParseProvisionUrl(L"http://127.0.0.1:65535/key", &port) && port == 65535,
      "valid loopback endpoint");
  for (const wchar_t* invalid :
       {L"http://localhost:80/key", L"https://127.0.0.1:80/key",
        L"http://127.0.0.1:0/key", L"http://127.0.0.1:65536/key",
        L"http://127.0.0.1:80/key?x", L"http://127.0.0.1:80/key/",
        L"http://127.0.0.1:80@evil/key", L"http://127.0.0.1:080/key",
        L"http://127.0.0.1:80/key#fragment", L"http://[::1]:80/key"})
    Check(!ParseProvisionUrl(invalid, &port), "reject noncanonical endpoint");
  Check(!ValidToken(L"token\r\nHeader: injection"), "reject header injection");
  Check(IsAbsoluteCachePath(L"C:\\temporary\\\u6d4b\u8bd5"),
        "Unicode absolute cache path");
  Check(!IsAbsoluteCachePath(L"C:relative") &&
            !IsAbsoluteCachePath(L"\\\\server\\share"),
        "reject relative and network cache paths");
  std::string json;
  AppendJsonString("\"\\\n\x01\xc3\xa9", 6, &json);
  Check(json == "\"\\\"\\\\\\u000a\\u0001\xc3\xa9\"", "exact JSON escaping");
  size_t length = 0;
  Check(BoundedUtf8(kTestAad, &length) && length == sizeof(kTestAad) - 1,
        "valid UTF8");
  Check(!BoundedUtf8("\xc0\xaf", &length), "reject malformed UTF8");
  SecretKey decoded;
  std::string hex(64, 'a');
  Check(DecodeResponse(hex.data(), hex.size(), &decoded), "hex response");
  hex[12] = 'A';
  Check(!DecodeResponse(hex.data(), hex.size(), &decoded),
        "reject uppercase response");
  Check(!DecodeResponse(hex.data(), 63, &decoded), "reject short response");
  hex[12] = 'a';
  hex.push_back('\n');
  Check(!DecodeResponse(hex.data(), hex.size(), &decoded),
        "reject response suffix");

  std::array<uint8_t, kKeySize> digest{};
  Check(ContextDigest(kTestId, sizeof(kTestId) - 1, kTestAad,
                      sizeof(kTestAad) - 1, &digest),
        "context SHA256");
  Check(Hex(digest.data(), digest.size()) ==
            "a7ed2ef909a5dec949d5c442afa9f031221c1138a4a157f1e4056dc3b0f023b8",
        "independent context digest vector");
  std::array<wchar_t, 32768> temporary{};
  DWORD count =
      GetTempPathW(static_cast<DWORD>(temporary.size()), temporary.data());
  Check(count > 0 && count < temporary.size(), "temporary directory path");
  std::array<uint8_t, 16> random{};
  Check(
      BCryptGenRandom(nullptr, random.data(), static_cast<ULONG>(random.size()),
                      BCRYPT_USE_SYSTEM_PREFERRED_RNG) >= 0,
      "temporary directory random");
  const std::string random_hex = Hex(random.data(), random.size());
  TestDirectory directory;
  directory.path = std::wstring(temporary.data(), count) +
                   L"shorebird-\u6d4b\u8bd5-" +
                   std::wstring(random_hex.begin(), random_hex.end());
  Check(CreateDirectoryW(directory.path.c_str(), nullptr) != FALSE,
        "create Unicode directory");
  Config config;
  config.cache_dir = directory.path;
  directory.cache = CachePath(config, digest);
  SecretKey input;
  for (size_t i = 0; i < input.bytes.size(); ++i)
    input.bytes[i] = static_cast<uint8_t>(i + 1);
  Check(WriteCachedKey(directory.cache, digest, &input),
        "atomic DPAPI cache write");
  Configure(directory.path, L"correct");
  Check(RegisterWindowsNativeCacheTestProvider() && g_test_provider,
        "public provider registration");
  Check(!RegisterWindowsNativeCacheTestProvider(),
        "reject duplicate registration");
  ExpectKey();
  RunColdRead(directory.path);
  SecretKey output;
  size_t written = 99;
  Check(!g_test_provider(kTestId, "different context", output.bytes.data(),
                         kKeySize, &written) &&
            written == 0,
        "offline unknown context fails");
  auto wrong_entropy = digest;
  wrong_entropy[0] ^= 1;
  Check(!ReadCachedKey(directory.cache, wrong_entropy, &output),
        "DPAPI entropy binds context");
  Check(
      !g_test_provider(kTestId, kTestAad, output.bytes.data(), 31, &written) &&
          written == 0,
      "reject undersized output");
  Check(!g_test_provider(nullptr, kTestAad, output.bytes.data(), kKeySize,
                         &written),
        "reject null context");

  g_config.mode = L"missing";
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "missing mode fails despite cached key");
  g_config.mode = L"wrong";
  output.bytes.fill(0xff);
  Check(g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                        &written) &&
            written == kKeySize,
        "wrong mode succeeds with key length");
  for (uint8_t byte : output.bytes) Check(byte == 0, "wrong mode zero key");
  g_config.mode = L"correct";
  ExpectKey();
  {
    FileHandle file(CreateFileW(directory.cache.c_str(), GENERIC_WRITE, 0,
                                nullptr, TRUNCATE_EXISTING,
                                FILE_ATTRIBUTE_NORMAL, nullptr));
    Check(file.get() != INVALID_HANDLE_VALUE, "open corruption fixture");
    const char invalid[] = "invalid DPAPI blob";
    DWORD wrote = 0;
    Check(WriteFile(file.get(), invalid, sizeof(invalid), &wrote, nullptr) &&
              wrote == sizeof(invalid),
          "write corruption fixture");
  }
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "corrupt offline cache fails closed");
  Check(WriteCachedKey(directory.cache, digest, &input),
        "replace corrupt encrypted cache atomically");
  ExpectKey();
  {
    FileHandle file(CreateFileW(directory.cache.c_str(), GENERIC_WRITE, 0,
                                nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                                nullptr));
    Check(file.get() != INVALID_HANDLE_VALUE, "open oversized fixture");
    LARGE_INTEGER size{};
    size.QuadPart = kMaxBlob + 1;
    Check(SetFilePointerEx(file.get(), size, nullptr, FILE_BEGIN) &&
              SetEndOfFile(file.get()),
          "write oversized fixture");
  }
  Check(!ReadCachedKey(directory.cache, digest, &output),
        "reject oversized encrypted blob");
  // RemoveDirectory fails if an atomic-write temporary file was leaked.
  Check(DeleteFileW(directory.cache.c_str()) != FALSE,
        "remove encrypted test cache");
  directory.cache.clear();
  Check(RemoveDirectoryW(directory.path.c_str()) != FALSE,
        "no leaked cache temporary files");
  directory.path.clear();
}
}  // namespace

extern "C" bool shorebird_set_patch_decryption_key_provider(
    FlutterShorebirdPatchDecryptionKeyProvider provider) {
  g_test_provider = provider;
  return true;
}

int wmain(int argc, wchar_t** argv) {
  try {
    if (argc == 3 && std::wstring(argv[1]) == L"--cold-read") {
      Configure(argv[2], L"correct");
      Check(RegisterWindowsNativeCacheTestProvider() && g_test_provider,
            "cold registration");
      ExpectKey();
    } else {
      Check(argc == 1, "test arguments");
      RunTests();
    }
    std::puts("Windows native cache contract tests passed");
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Windows native cache test failed: %s\n",
                 error.what());
    return 1;
  }
}
