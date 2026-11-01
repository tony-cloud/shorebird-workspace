#ifndef SHOREBIRD_NATIVE_VARIANT_CONTEXT_H_
#define SHOREBIRD_NATIVE_VARIANT_CONTEXT_H_
// Test-only strict bounded decoder. These contexts came from actual signed
// assignments; no JSON string is ever interpreted as a command or filename.
#include <cstdlib>
#include <ctime>
#include <fstream>
#ifdef _WIN32
#include <filesystem>
#endif
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace native_variant {
struct Json {
  char type = 'n';
  std::string text;
  std::map<std::string, Json> object;
  std::vector<Json> array;
  bool operator==(const Json& rhs) const {
    return type == rhs.type && text == rhs.text && object == rhs.object && array == rhs.array;
  }
  const Json& at(const std::string& key) const { return object.at(key); }
  std::string string() const {
    if (type != 's') throw std::runtime_error("Expected string");
    return text;
  }
};
class Decoder {
 public:
  explicit Decoder(std::string source) : source_(std::move(source)) {
    if (source_.size() > 65536) throw std::runtime_error("Context too large");
  }
  Json parse() {
    Json value = read(0);
    space();
    if (position_ != source_.size()) throw std::runtime_error("Trailing JSON");
    return value;
  }
 private:
  std::string source_;
  size_t position_ = 0, nodes_ = 0;
  void space() { while (position_ < source_.size() && std::string(" \n\r\t").find(source_[position_]) != std::string::npos) ++position_; }
  char take() {
    if (position_ == source_.size()) throw std::runtime_error("Truncated JSON");
    return source_[position_++];
  }
  std::string string() {
    if (take() != '"') throw std::runtime_error("Expected quote");
    std::string out;
    for (;;) {
      unsigned char c = static_cast<unsigned char>(take());
      if (c == '"') return out;
      if (c < 32) throw std::runtime_error("Control in string");
      if (c == '\\') {
        char escaped = take();
        switch (escaped) {
          case '"': case '\\': case '/': out += escaped; break;
          case 'b': out += '\b'; break;
          case 'f': out += '\f'; break;
          case 'n': out += '\n'; break;
          case 'r': out += '\r'; break;
          case 't': out += '\t'; break;
          case 'u': {
            unsigned int code = 0;
            for (int i = 0; i < 4; ++i) {
              char hex = take();
              const std::string digits = "0123456789abcdef";
              size_t n = digits.find(hex >= 'A' && hex <= 'F' ? hex + ('a'-'A') : hex);
              if (n == std::string::npos) throw std::runtime_error("Invalid unicode");
              code = code * 16 + static_cast<unsigned int>(n);
            }
            // This disposable fixture's configured scopes use ASCII IDs only.
            if (code == 0 || code > 127) throw std::runtime_error("Non-ASCII fixture escape");
            out += static_cast<char>(code); break;
          }
          default: throw std::runtime_error("Invalid escape");
        }
      } else out += static_cast<char>(c);
    }
  }
  Json read(int depth) {
    if (depth > 24 || ++nodes_ > 4096) throw std::runtime_error("JSON bound");
    space();
    if (position_ == source_.size()) throw std::runtime_error("Missing JSON");
    Json result;
    char c = source_[position_];
    if (c == '{') {
      ++position_; result.type = 'o'; space();
      if (position_ < source_.size() && source_[position_] == '}') { ++position_; return result; }
      for (;;) {
        space(); std::string key = string(); space();
        if (take() != ':') throw std::runtime_error("Missing colon");
        if (!result.object.emplace(key, read(depth+1)).second) throw std::runtime_error("Duplicate key");
        space(); char separator = take();
        if (separator == '}') return result;
        if (separator != ',') throw std::runtime_error("Missing comma");
      }
    }
    if (c == '[') {
      ++position_; result.type = 'a'; space();
      if (position_ < source_.size() && source_[position_] == ']') { ++position_; return result; }
      for (;;) {
        result.array.push_back(read(depth+1)); space(); char separator = take();
        if (separator == ']') return result;
        if (separator != ',') throw std::runtime_error("Missing comma");
      }
    }
    if (c == '"') { result.type = 's'; result.text = string(); return result; }
    for (const auto& literal : {std::string("null"), std::string("true"), std::string("false")}) {
      if (source_.compare(position_, literal.size(), literal) == 0) {
        position_ += literal.size(); result.type = literal == "null" ? 'n' : 'b'; result.text = literal; return result;
      }
    }
    if (c >= '0' && c <= '9') {
      result.type = 'i';
      do { result.text += take(); } while (position_ < source_.size() && source_[position_] >= '0' && source_[position_] <= '9');
      if (result.text.size() > 19 || (result.text.size() > 1 && result.text[0] == '0')) throw std::runtime_error("Invalid integer");
      return result;
    }
    throw std::runtime_error("Unexpected JSON token");
  }
};
inline std::string bounded(const char* text, size_t maximum) {
  if (!text) throw std::runtime_error("Missing context");
  size_t size = 0;
  while (size <= maximum && text[size]) ++size;
  if (size > maximum) throw std::runtime_error("Context bound");
  return std::string(text, size);
}
inline bool hex64(const std::string& value) {
  return value.size() == 64 && value.find_first_not_of("0123456789abcdef") == std::string::npos;
}
inline unsigned long long lease_seconds(const Json& value) {
  const std::string text = value.string();
  if (text.empty() || text.size() > 12 || (text.size() > 1 && text[0] == '0') ||
      text.find_first_not_of("0123456789") != std::string::npos ||
      (text.size() == 12 && text > "253402300799")) throw std::runtime_error("Invalid lease seconds");
  return std::stoull(text);
}
inline Json validate(const char* id, const char* aad, const char* context) {
  const std::string key = bounded(id,128);
  if (key.size() != 16 || key.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") != std::string::npos) throw std::runtime_error("Key ID");
  Json actual = Decoder(bounded(context,65536)).parse();
  Json allowed = Decoder(bounded(std::getenv("SHOREBIRD_VARIANT_CONTEXTS"),65536)).parse();
  bool found = false;
  if (allowed.type != 'a') throw std::runtime_error("Missing authenticated grants");
  for (const Json& candidate : allowed.array) if (candidate == actual) found = true;
  if (!found || actual.type != 'o' || actual.object.size() != 3 || actual.at("version").type != 'i' || actual.at("version").text != "2" || !hex64(actual.at("selection_digest").string())) throw std::runtime_error("Unknown grant");
  std::string text = bounded(aad,8192);
  const std::vector<std::string> names = {"app_id","release_id","patch_number","patch_hash","transport_hash","platform","arch","device_id_hash"};
  const std::string header = "open-aot-patch-delivery-v2";
  if (text.compare(0,header.size()+1,header+"\n") != 0) throw std::runtime_error("AAD header");
  size_t begin = header.size()+1;
  std::map<std::string,std::string> fields;
  for (size_t i=0;i<names.size();++i) {
    size_t end = text.find('\n',begin);
    if ((end == std::string::npos) != (i+1 == names.size())) throw std::runtime_error("AAD framing");
    std::string line = text.substr(begin,end == std::string::npos ? end : end-begin);
    if (line.compare(0,names[i].size()+1,names[i]+"=") != 0) throw std::runtime_error("AAD order");
    fields[names[i]] = line.substr(names[i].size()+1); begin = end+1;
  }
  const Json& descriptor = actual.at("selection");
  const Json& scope = descriptor.at("scope");
  const Json& target = descriptor.at("target");
  const std::string variant = descriptor.at("variant_id").string();
  const std::time_t clock = std::time(nullptr);
  const auto start = lease_seconds(descriptor.at("not_before"));
  const Json& expiration = descriptor.at("offline_valid_until");
  if (clock < 0 || static_cast<unsigned long long>(clock) < start ||
      (expiration.type != 'n' && (lease_seconds(expiration) <= start ||
       static_cast<unsigned long long>(clock) >= lease_seconds(expiration)))) throw std::runtime_error("Grant lease unavailable");
  if ((variant != "pro" && variant != "enterprise") || descriptor.at("license_type").string() != variant || descriptor.at("channel").string() != "stable" ||
      scope.at("runtime_profile").string() != "native-aot" || scope.at("host_abi_hash").type != 'n' || !hex64(scope.at("base_artifact_hash").string()) ||
      target.at("kind").string() != "patch" || target.at("encryption").string() != "transport-aes-256-gcm" || target.at("artifact_key_id").type != 'n' ||
      target.at("patch_number").string() != fields["patch_number"] || target.at("artifact_hash").string() != fields["patch_hash"] || !hex64(fields["transport_hash"])) throw std::runtime_error("Native grant binding");
  for (const char* name : {"app_id","platform","arch"}) if (scope.at(name).string() != fields[name]) throw std::runtime_error("Scope/AAD binding");
  if (scope.at("client_id_hash").string() != fields["device_id_hash"]) throw std::runtime_error("Installation binding");
  return actual;
}
inline bool receipt(const Json& context, const char* key_id, unsigned long pid) {
  static std::mutex receipt_mutex;
  std::lock_guard<std::mutex> lock(receipt_mutex);
  const Json& descriptor = context.at("selection");
  const std::string nonce = bounded(std::getenv("SHOREBIRD_E2E_RECEIPT"),128);
  if (nonce.empty() || nonce.find_first_not_of("0123456789abcdef") != std::string::npos) return false;
#ifdef _WIN32
  const wchar_t* wide_path = _wgetenv(L"SHOREBIRD_VARIANT_PROVIDER_RECEIPT");
  if (!wide_path) return false;
  std::ofstream output(std::filesystem::path(wide_path), std::ios::app);
#else
  std::ofstream output(bounded(std::getenv("SHOREBIRD_VARIANT_PROVIDER_RECEIPT"),4096), std::ios::app);
#endif
  output << "{\"nonce\":\"" << nonce << "\",\"pid\":" << pid
         << ",\"epoch\":\"" << descriptor.at("epoch").string()
         << "\",\"digest\":\"" << context.at("selection_digest").string()
         << "\",\"variant_id\":\"" << descriptor.at("variant_id").string()
         << "\",\"license_type\":\"" << descriptor.at("license_type").string()
         << "\",\"patch_number\":\"" << descriptor.at("target").at("patch_number").string()
         << "\",\"key_id\":\"" << key_id << "\"}\n";
  output.flush();
  return output.good();
}
}  // namespace native_variant
#endif
