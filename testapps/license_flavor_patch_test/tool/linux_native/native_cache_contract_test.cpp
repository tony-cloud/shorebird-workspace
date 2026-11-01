// Standalone Linux probe. Includes the implementation so helpers remain private
// in the GTK Runner. Do not also compile runner_patch_key_provider.cpp.
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <thread>

#include "runner_patch_key_provider.cpp"

namespace {
FlutterShorebirdPatchDecryptionKeyProvider g_test_provider = nullptr;
bool g_accept_registration = true;
constexpr char kTestId[] = "fixture-key";
constexpr char kTestAad[] = "app=temporary\nclient=\xc3\xa9";

void Check(bool condition, const char* label) {
  if (!condition) throw std::runtime_error(label);
}

void SetEnvironment(const char* name, const char* value) {
  Check((value == nullptr ? unsetenv(name) : setenv(name, value, 1)) == 0,
        "set fixture environment");
}

void OfflineEnvironment() {
  SetEnvironment("SHOREBIRD_E2E_KEY_MODE", "correct");
  SetEnvironment("SHOREBIRD_E2E_PROVISION_URL", nullptr);
  SetEnvironment("SHOREBIRD_E2E_PROVISION_TOKEN", nullptr);
}

struct TestKey {
  key_serial_t keyring = 0;
  key_serial_t serial = 0;
  ~TestKey() {
    if (serial > 0) {
      keyctl_revoke(serial);
      keyctl_unlink(serial, keyring);
    }
  }
};

void ExpectFixture() {
  SecretKey output;
  size_t written = 99;
  Check(g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                        &written) &&
            written == kKeySize,
        "offline fixture lookup");
  for (size_t i = 0; i < kKeySize; ++i)
    Check(output.bytes[i] == static_cast<uint8_t>(i + 1),
          "offline fixture key bytes");
}

void FreshProcessRead() {
  const pid_t child = fork();
  Check(child >= 0, "fork offline reader");
  if (child == 0) {
    execl("/proc/self/exe", "native_cache_contract_test", "--fixture-cold-read",
          static_cast<char*>(nullptr));
    _exit(127);
  }
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(15);
  int status = 0;
  while (std::chrono::steady_clock::now() < deadline) {
    const pid_t result = waitpid(child, &status, WNOHANG);
    if (result == child) {
      Check(WIFEXITED(status) && WEXITSTATUS(status) == 0,
            "fresh-process offline result");
      return;
    }
    if (result < 0 && errno != EINTR) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  kill(child, SIGKILL);
  while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
  }
  Check(false, "fresh-process offline timeout/wait failure");
}

void SelfTest() {
  Check(ParseProvisionUrl("http://127.0.0.1:65535/key"), "valid endpoint");
  for (const char* invalid :
       {"http://localhost:80/key", "https://127.0.0.1:80/key",
        "http://127.0.0.1:0/key", "http://127.0.0.1:65536/key",
        "http://127.0.0.1:080/key", "http://127.0.0.1:80/key?x",
        "http://127.0.0.1:80/key/", "http://127.0.0.1:80/key#fragment",
        "http://127.0.0.1:80@evil/key", "http://[::1]:80/key"})
    Check(!ParseProvisionUrl(invalid), "reject noncanonical endpoint");
  Check(ValidToken("safe-token_123.~") &&
            !ValidToken("token\r\nHeader: injection") &&
            !ValidToken(std::string(513, 'x')),
        "bounded safe token");
  unsigned int number = 0;
  for (const char* invalid :
       {"", "0", "-3", "+3", "03", "3x", "2147483648", "99999999999999999"})
    Check(!PositiveDecimal(invalid, INT_MAX, &number), "strict keyring serial");
  size_t length = 0;
  Check(BoundedUtf8(kTestAad, &length) && length == sizeof(kTestAad) - 1,
        "valid bounded UTF8");
  const std::string oversized(kMaxRequest + 1, 'x');
  Check(!BoundedUtf8(nullptr, &length) && !BoundedUtf8("", &length) &&
            !BoundedUtf8("\xc0\xaf", &length) &&
            !BoundedUtf8(oversized.c_str(), &length),
        "reject invalid context");
  std::string json;
  AppendJsonString("\"\\\n\x01\xc3\xa9", 6, &json);
  Check(json == "\"\\\"\\\\\\u000a\\u0001\xc3\xa9\"", "exact JSON escaping");
  SecretKey output;
  std::string response(64, 'a');
  Check(DecodeResponse(response.data(), response.size(), &output),
        "valid response");
  response[3] = 'A';
  Check(!DecodeResponse(response.data(), response.size(), &output),
        "reject uppercase response");
  response[3] = 'a';
  Check(!DecodeResponse(response.data(), 63, &output), "reject short response");
  response.push_back('\n');
  Check(!DecodeResponse(response.data(), response.size(), &output),
        "reject response suffix");
  ResponseBytes bounded_response;
  std::array<char, 66> too_large{};
  Check(ReceiveResponse(too_large.data(), 1, too_large.size(),
                        &bounded_response) == 0 &&
            ReceiveResponse(too_large.data(), static_cast<size_t>(-1), 2,
                            &bounded_response) == 0,
        "bound response callback and multiplication");

  const std::string description = ContextDescription(
      kTestId, sizeof(kTestId) - 1, kTestAad, sizeof(kTestAad) - 1);
  Check(description ==
            "shorebird-e2e-v1:"
            "a7ed2ef909a5dec949d5c442afa9f031221c1138a4a157f1e4056dc3b0f023b8",
        "independent context digest vector");
  Check(ContextDescription("a", 1, "bc", 2) !=
            ContextDescription("ab", 2, "c", 1),
        "NUL separates exact context components");

  std::string ring_environment;
  Check(ReadEnvironment("SHOREBIRD_E2E_KEYRING_ID", &ring_environment) &&
            PositiveDecimal(ring_environment, INT_MAX, &number),
        "owned session keyring environment required");
  const key_serial_t ring = static_cast<key_serial_t>(number);
  Check(keyctl_get_keyring_ID(KEY_SPEC_SESSION_KEYRING, 0) == ring,
        "real inherited kernel session keyring required");
  OfflineEnvironment();
  for (const char* invalid : {"0", "-3", "03", "2147483648"}) {
    SetEnvironment("SHOREBIRD_E2E_KEYRING_ID", invalid);
    Check(!RegisterLinuxNativeCacheTestProvider(),
          "reject invalid keyring config");
  }
  const key_serial_t other = ring == INT_MAX ? ring - 1 : ring + 1;
  SetEnvironment("SHOREBIRD_E2E_KEYRING_ID", std::to_string(other).c_str());
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject non-session keyring");
  SetEnvironment("SHOREBIRD_E2E_KEYRING_ID", ring_environment.c_str());
  SetEnvironment("SHOREBIRD_E2E_KEY_MODE", "typo");
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject unknown mode");
  OfflineEnvironment();
  SetEnvironment("SHOREBIRD_E2E_PROVISION_URL", "http://127.0.0.1:80/key");
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject URL without token");
  SetEnvironment("SHOREBIRD_E2E_PROVISION_TOKEN", "unsafe token");
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject unsafe token config");
  SetEnvironment("SHOREBIRD_E2E_PROVISION_TOKEN", "safe");
  SetEnvironment("SHOREBIRD_E2E_PROVISION_URL", "http://localhost:80/key");
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject non-loopback config");
  OfflineEnvironment();
  SetEnvironment("SHOREBIRD_E2E_PROVISION_TOKEN", "safe");
  Check(!RegisterLinuxNativeCacheTestProvider(), "reject token without URL");
  OfflineEnvironment();
  g_accept_registration = false;
  Check(!RegisterLinuxNativeCacheTestProvider() && !g_registered,
        "engine registration rejection fails closed");
  g_accept_registration = true;
  Check(RegisterLinuxNativeCacheTestProvider() && g_test_provider,
        "public callback registration");
  Check(!RegisterLinuxNativeCacheTestProvider(),
        "reject duplicate registration");

  TestKey fixture;
  fixture.keyring = ring;
  SecretKey input;
  for (size_t i = 0; i < kKeySize; ++i) input.bytes[i] = i + 1;
  Check(WriteCachedKey(ring, description, input), "write real kernel user key");
  fixture.serial = keyctl_search(ring, "user", description.c_str(), 0);
  Check(fixture.serial > 0 && RestrictedKey(fixture.serial, description),
        "possessor-only context-bound user key");
  ExpectFixture();
  FreshProcessRead();
  size_t written = 99;
  Check(!g_test_provider(kTestId, "different context", output.bytes.data(),
                         kKeySize, &written) &&
            written == 0,
        "offline changed AAD fails");
  Check(!g_test_provider("different id", kTestAad, output.bytes.data(),
                         kKeySize, &written) &&
            written == 0,
        "offline changed key id fails");
  Check(
      !g_test_provider(kTestId, kTestAad, output.bytes.data(), 31, &written) &&
          written == 0,
      "undersized output fails");
  Check(!g_test_provider(nullptr, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "null context fails");
  Check(!g_test_provider(kTestId, "\xc0\xaf", output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "malformed UTF8 fails at ABI");
  Check(!g_test_provider(kTestId, kTestAad, nullptr, kKeySize, &written) &&
            written == 0 &&
            !g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                             nullptr),
        "null ABI output arguments fail");
  Check(keyctl_setperm(fixture.serial, KEY_POS_ALL | KEY_USR_READ) == 0,
        "mutate key permissions");
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "reject non-possessor permission bits");
  Check(keyctl_setperm(fixture.serial, KEY_POS_ALL) == 0 &&
            keyctl_update(fixture.serial, input.bytes.data(), 31) == 0,
        "mutate payload length");
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "reject malformed cached payload");
  Check(keyctl_update(fixture.serial, input.bytes.data(), kKeySize) == 0,
        "restore fixture payload");
  const key_serial_t saved_ring = g_config.keyring;
  g_config.keyring =
      0;  // No usable keyring: failure modes must not consult it.
  g_config.mode = "missing";
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "missing mode bypasses retained key");
  g_config.mode = "wrong";
  output.bytes.fill(0xff);
  Check(g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                        &written) &&
            written == kKeySize,
        "wrong mode bypasses keyring");
  for (uint8_t byte : output.bytes) Check(byte == 0, "wrong-mode bytes");
  g_config.keyring = saved_ring;
  g_config.mode = "correct";
  ExpectFixture();
  Check(keyctl_revoke(fixture.serial) == 0, "revoke fixture key");
  Check(!g_test_provider(kTestId, kTestAad, output.bytes.data(), kKeySize,
                         &written) &&
            written == 0,
        "revoked key cannot be read offline");
}

std::string ReadAad(const char* path) {
  std::ifstream stream(path, std::ios::binary);
  Check(stream.is_open(), "open AAD input");
  std::array<char, kMaxRequest + 1> bytes{};
  stream.read(bytes.data(), bytes.size());
  Check(!stream.bad(), "read AAD input");
  const size_t size = static_cast<size_t>(stream.gcount());
  Check(size > 0 && size <= kMaxRequest &&
            std::memchr(bytes.data(), 0, size) == nullptr,
        "bounded AAD input without NUL");
  return std::string(bytes.data(), size);
}

void ReadContext(const char* id, const char* path, bool unavailable) {
  const std::string aad = ReadAad(path);
  Check(RegisterLinuxNativeCacheTestProvider() && g_test_provider,
        "context probe registration");
  SecretKey output;
  size_t written = 99;
  const bool found = g_test_provider(id, aad.c_str(), output.bytes.data(),
                                     output.bytes.size(), &written);
  if (unavailable) {
    Check(!found && written == 0, "expected unavailable key");
    return;
  }
  SecretKey expected;
  Check(std::fread(expected.bytes.data(), 1, kKeySize, stdin) == kKeySize &&
            std::fgetc(stdin) == EOF && !std::ferror(stdin),
        "expected key must be exactly 32 bytes on stdin");
  unsigned int difference = 0;
  for (size_t i = 0; i < kKeySize; ++i)
    difference |= output.bytes[i] ^ expected.bytes[i];
  Check(found && written == kKeySize && difference == 0,
        "returned key differs from expected bytes");
}
}  // namespace

extern "C" bool shorebird_set_patch_decryption_key_provider(
    FlutterShorebirdPatchDecryptionKeyProvider provider) {
  if (!g_accept_registration) return false;
  g_test_provider = provider;
  return true;
}

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::strcmp(argv[1], "--self-test") == 0) {
      SelfTest();
    } else if (argc == 2 && std::strcmp(argv[1], "--fixture-cold-read") == 0) {
      Check(RegisterLinuxNativeCacheTestProvider(), "cold registration");
      ExpectFixture();
    } else if (argc == 4 &&
               (std::strcmp(argv[1], "--read-context") == 0 ||
                std::strcmp(argv[1], "--expect-unavailable") == 0)) {
      ReadContext(argv[2], argv[3],
                  std::strcmp(argv[1], "--expect-unavailable") == 0);
    } else {
      std::fprintf(stderr,
                   "usage: native_cache_contract_test --self-test | "
                   "--read-context KEY_ID AAD_FILE | "
                   "--expect-unavailable KEY_ID AAD_FILE\n");
      return 2;
    }
    return 0;
  } catch (const std::exception& error) {
    // Labels never include key bytes, bearer tokens, URLs or context values.
    std::fprintf(stderr, "native cache contract failed: %s\n", error.what());
    return 1;
  } catch (...) {
    std::fprintf(stderr, "native cache contract failed\n");
    return 1;
  }
}
