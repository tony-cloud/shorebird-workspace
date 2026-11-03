import Flutter
import OSLog
import Security
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  override init() {
    super.init()
    // Device-test provisioning is supplied by the test runner, never bundled
    // into the app or uploaded as release metadata. Subsequent cold launches
    // retrieve the provisioned key from this app's device-only Keychain item.
    PatchTestKeys.provisionFromTestEnvironment()
    let registered = FlutterDartProject.setShorebirdAotPatchKeyProvider { keyID in
      PatchTestKeys.read(keyID: keyID)
    }
    precondition(registered, "Patch key provider must precede engine creation")
  }

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    // Keep the display on so frame timing is collected during the benchmark.
    application.isIdleTimerDisabled = true
    // Diagnostics: dump this process's own unified log (engine FML_LOG output)
    // to Documents/oslog.txt shortly after launch, for copy-back via devicectl.
    DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
      do {
        let store = try OSLogStore(scope: .currentProcessIdentifier)
        let entries = try store.getEntries()
        var out = ""
        for case let e as OSLogEntryLog in entries { out += "\(e.date) [\(e.subsystem)] \(e.composedMessage)\n" }
        let docs = NSSearchPathForDirectoriesInDomains(.documentDirectory, .userDomainMask, true)[0]
        try out.write(toFile: docs + "/oslog.txt", atomically: true, encoding: .utf8)
      } catch {
        NSLog("PERFBENCH oslog dump failed: %@", "\(error)")
      }
    }
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
  }
}

private enum PatchTestKeys {
  static let service = "open-shorebird-patch-perf-bench"

  static func query(keyID: String) -> [String: Any] {
    return [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: keyID,
    ]
  }

  static func provisionFromTestEnvironment() {
    let environment = ProcessInfo.processInfo.environment
    guard let hex = environment["OPEN_SHOREBIRD_TEST_KEY_HEX"],
          let keyID = environment["OPEN_SHOREBIRD_TEST_KEY_ID"] else { return }
    precondition(hex.count == 64 && !keyID.isEmpty, "Invalid test provisioning")
    var bytes = [UInt8]()
    var index = hex.startIndex
    while index < hex.endIndex {
      let end = hex.index(index, offsetBy: 2)
      guard let byte = UInt8(hex[index..<end], radix: 16) else {
        preconditionFailure("Invalid test provisioning")
      }
      bytes.append(byte)
      index = end
    }
    let identity = query(keyID: keyID)
    SecItemDelete(identity as CFDictionary)
    var item = identity
    item[kSecValueData as String] = Data(bytes)
    item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    precondition(SecItemAdd(item as CFDictionary, nil) == errSecSuccess,
                 "Could not provision test patch key")
  }

  // Benchmark-only convenience: build_key.sh substitutes the disposable local
  // test key here at build time (the committed placeholder is empty), because
  // devicectl launch environment is not reliably delivered to the process.
  static let embeddedKeyHex = "__PERF_KEY_HEX__"

  static func embeddedKey() -> Data? {
    let hex = embeddedKeyHex
    guard hex.count == 64 else { return nil }
    var bytes = [UInt8]()
    var index = hex.startIndex
    while index < hex.endIndex {
      let end = hex.index(index, offsetBy: 2)
      guard let byte = UInt8(hex[index..<end], radix: 16) else { return nil }
      bytes.append(byte)
      index = end
    }
    return Data(bytes)
  }

  static func read(keyID: String) -> Data? {
    if let key = embeddedKey() {
      NSLog("PERFBENCH key provider: embedded key for %@", keyID)
      return key
    }
    var request = query(keyID: keyID)
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
          let data = result as? Data, data.count == 32 else { return nil }
    return data
  }
}
