import Flutter
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
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
  }
}

private enum PatchTestKeys {
  static let service = "open-shorebird-license-patch-device-test"

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

  static func read(keyID: String) -> Data? {
    var request = query(keyID: keyID)
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
          let data = result as? Data, data.count == 32 else { return nil }
    return data
  }
}
