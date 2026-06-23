enum LicenseType { free, pro, enterprise }

LicenseType currentLicenseType() {
  const license = String.fromEnvironment('LICENSE_TYPE', defaultValue: 'free');
  return switch (license) {
    'pro' => LicenseType.pro,
    'enterprise' => LicenseType.enterprise,
    _ => LicenseType.free,
  };
}

bool get proFeatureEnabled =>
    currentLicenseType() == LicenseType.pro ||
    currentLicenseType() == LicenseType.enterprise;
