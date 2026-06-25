#!/usr/bin/env ruby
# frozen_string_literal: true

require 'tempfile'
require 'yaml'
require 'open3'
require 'pathname'

args = ARGV.dup
require_tracked = false
require_clean = false
args.delete_if do |arg|
  case arg
  when '--require-tracked'
    require_tracked = true
    true
  when '--require-clean'
    require_clean = true
    true
  when '--require-upload-ready'
    require_tracked = true
    require_clean = true
    true
  else
    false
  end
end

workflow_path = args.fetch(0)
repo_root = File.expand_path('../..', File.dirname(workflow_path))
workflow_text = File.read(workflow_path)
workflow = YAML.load_file(workflow_path)
jobs = workflow.fetch('jobs')

def fail!(message)
  warn "workflow contract failed: #{message}"
  exit 70
end

def assert!(condition, message)
  fail!(message) unless condition
end

def job_runs(job)
  job.fetch('steps', []).each_with_object([]) do |step, runs|
    runs << step['run'] if step.key?('run')
  end
end

def upload_names(job)
  job.fetch('steps', []).each_with_object([]) do |step, names|
    next unless step['uses'].to_s.start_with?('actions/upload-artifact@')

    names << step.fetch('with', {}).fetch('name')
  end
end

def step_uses(job, action)
  job.fetch('steps', []).any? { |step| step['uses'].to_s == action }
end

def matrix_include(job)
  job.fetch('strategy').fetch('matrix').fetch('include')
end

def repo_path(repo_root, path)
  File.join(repo_root, path)
end

def read_repo_file(repo_root, path)
  File.read(repo_path(repo_root, path))
end

def capture_command(*argv)
  stdout, _stderr, status = Open3.capture3(*argv)
  [status.success?, stdout.strip]
end

def git_toplevel_for(path)
  dir = File.directory?(path) ? path : File.dirname(path)
  success, stdout = capture_command('git', '-C', dir, 'rev-parse', '--show-toplevel')
  return nil unless success

  File.expand_path(stdout)
end

def relative_path(from, to)
  Pathname.new(File.expand_path(to)).relative_path_from(Pathname.new(File.expand_path(from))).to_s
end

def display_repo(repo_root, git_root)
  return '.' if File.expand_path(git_root) == File.expand_path(repo_root)

  relative_path(repo_root, git_root)
end

def required_git_repos(paths)
  paths.each_with_object({}) do |path, repos|
    git_root = git_toplevel_for(path)
    repos[git_root] = true unless git_root.nil?
  end.keys
end

scripts = []
jobs.each_value do |job|
  job.fetch('steps', []).each do |step|
    scripts << step['run'] if step.key?('run')
  end
end

scripts.each_with_index do |script, index|
  Tempfile.create(["open-shorebird-ci-run-#{index}", '.sh']) do |file|
    file.write(script.gsub(/\$\{\{[^}]+\}\}/, 'x'))
    file.flush
    system('bash', '-n', file.path, exception: true)
  end
end

required_files = %w[
  .gitmodules
  README.md
  docs/CI.md
  docs/PLATFORM_TESTING.md
  docs/REPOSITORIES.md
  dart-sdk/DEPS
  shorebird/README.md
  shorebird/OPEN_SOURCE_REPLACEMENTS.md
  shorebird/docs/account/api-keys/README.md
  shorebird/docs/code-push/troubleshooting/README.md
  shorebird/docs/getting-started/flutter-version/README.md
  scripts/assemble_artifact_mirror.sh
  scripts/android_runtime_patch_smoke.sh
  scripts/bootstrap_linux.sh
  scripts/bootstrap_macos.sh
  scripts/check_ci_capacity.sh
  scripts/free_ci_disk_linux.sh
  scripts/linux_runtime_patch_smoke.sh
  scripts/verify_open_infrastructure_defaults.sh
  scripts/safe_extract_tar.py
  scripts/sync_flutter_prebuilt_dart_sdk.sh
  scripts/sync_open_sources.sh
  scripts/validate_artifact_mirror.py
  scripts/validate_release_manifest.py
  scripts/verify_assemble_artifact_mirror.sh
  scripts/verify_artifact_mirror_validator.sh
  scripts/verify_artifact_mirror_workflow_assembly.sh
  scripts/verify_ci_workflow.rb
  scripts/verify_ci_workflow.sh
  scripts/verify_ci_capacity.sh
  scripts/verify_dart_tool_sdk.sh
  scripts/verify_dart_sdk_args.sh
  scripts/verify_engine_args.sh
  scripts/verify_hosted_full_sdk_build.sh
  scripts/verify_ios_interpreter_route.sh
  scripts/verify_ios_interpreter_route_validator.sh
  scripts/verify_downloaded_release_artifacts.sh
  scripts/verify_powershell_open_defaults.sh
  scripts/verify_release_manifest.sh
  scripts/verify_sync_open_sources.sh
  scripts/verify_upload_readiness.sh
  scripts/verify_write_sha256.sh
  scripts/write_artifact_manifest.py
  scripts/write_release_manifest.py
  scripts/write_gclient.sh
  scripts/write_sha256.py
  scripts/write_sha256.sh
  shorebird/bin/shorebird.ps1
  shorebird/third_party/flutter/bin/internal/shared.sh
  shorebird/packages/shorebird_cli/lib/src/cache.dart
  shorebird/packages/shorebird_cli/lib/src/commands/doctor_command.dart
  shorebird/packages/shorebird_cli/lib/src/network_checker.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_cli_command_runner.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_documentation.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_env.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_flutter.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_process.dart
  shorebird/packages/shorebird_cli/lib/src/shorebird_web_console.dart
  shorebird/packages/shorebird_code_push_client/lib/src/code_push_client.dart
  shorebird/packages/shorebird_code_push_protocol/tool/gen.dart
  shorebird/packages/shorebird_code_push_protocol/README.md
  shorebird-server/internal/api/handlers/openapi.yaml
  shorebird-server/internal/api/handlers/router.go
  shorebird/packages/artifact_proxy/lib/src/artifact_manifest_client.dart
  shorebird/packages/artifact_proxy/lib/src/artifact_proxy.dart
  shorebird/packages/artifact_proxy/lib/config.dart
  flutter/bin/internal/update_dart_sdk.ps1
  flutter/bin/internal/update_dart_sdk.sh
  flutter/packages/flutter_tools/lib/src/cache.dart
  flutter/packages/flutter_tools/lib/src/http_host_validator.dart
  flutter/packages/flutter_tools/pubspec.yaml
  flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt
  flutter/packages/flutter_tools/gradle/aar_init_script.gradle
  flutter/DEPS
  flutter/engine/src/flutter/build/dart/BUILD.gn
  flutter/dev/bots/post_process_docs.dart
  flutter/dev/bots/unpublish_package.dart
  flutter/dev/integration_tests/pure_android_host_apps/android_host_app_v2_embedding/settings.gradle
  flutter/dev/integration_tests/pure_android_host_apps/host_app_kotlin_gradle_dsl/settings.gradle.kts
  flutter/dev/tools/create_api_docs.dart
  flutter/engine/src/flutter/build/zip_bundle.gni
  flutter/engine/src/flutter/pubspec.yaml
  flutter/engine/src/flutter/runtime/dart_isolate.cc
  flutter/engine/src/flutter/runtime/shorebird/BUILD.gn
  flutter/engine/src/flutter/shell/platform/embedder/BUILD.gn
  flutter/engine/src/flutter/lib/web_ui/dev/steps/copy_artifacts_step.dart
  flutter/packages/shorebird_tests/test/shorebird_tests.dart
  updater/library/src/config.rs
]
missing_files = required_files.reject { |path| File.file?(repo_path(repo_root, path)) }
assert!(missing_files.empty?, "missing CI support files: #{missing_files.join(', ')}")

required_executables = required_files.select { |path| path.start_with?('scripts/') }
missing_executable_bits = required_executables.reject do |path|
  File.executable?(repo_path(repo_root, path))
end
assert!(
  missing_executable_bits.empty?,
  "missing executable bit on CI support scripts: #{missing_executable_bits.join(', ')}"
)

required_git_paths = [workflow_path] + required_files.map { |path| repo_path(repo_root, path) }
if require_tracked
  untracked_required_files = []
  required_git_paths.each do |path|
    git_root = git_toplevel_for(path)
    display_path = path.start_with?(repo_root) ? relative_path(repo_root, path) : path
    if git_root.nil?
      untracked_required_files << "#{display_path}: not inside a git checkout"
      next
    end

    path_in_repo = relative_path(git_root, path)
    tracked, = capture_command(
      'git',
      '-C',
      git_root,
      'ls-files',
      '--error-unmatch',
      '--',
      path_in_repo
    )
    next if tracked

    untracked_required_files << "#{display_path}: not tracked in #{display_repo(repo_root, git_root)}"
  end
  assert!(
    untracked_required_files.empty?,
    "required upload files are not tracked: #{untracked_required_files.join(', ')}"
  )
end

if require_clean
  dirty_repos = []
  required_git_repos(required_git_paths).each do |git_root|
    status_ok, status = capture_command('git', '-C', git_root, 'status', '--porcelain')
    unless status_ok
      dirty_repos << "#{display_repo(repo_root, git_root)}: unable to read git status"
      next
    end
    next if status.empty?

    dirty_repos << "#{display_repo(repo_root, git_root)}: has uncommitted changes"
  end
  assert!(
    dirty_repos.empty?,
    "required upload repositories are dirty: #{dirty_repos.join(', ')}"
  )
end

readme = read_repo_file(repo_root, 'README.md')
ci_doc = read_repo_file(repo_root, 'docs/CI.md')
platform_doc = read_repo_file(repo_root, 'docs/PLATFORM_TESTING.md')
repositories_doc = read_repo_file(repo_root, 'docs/REPOSITORIES.md')
open_replacements_doc = read_repo_file(repo_root, 'shorebird/OPEN_SOURCE_REPLACEMENTS.md')
shorebird_readme = read_repo_file(repo_root, 'shorebird/README.md')
gitmodules = read_repo_file(repo_root, '.gitmodules')
shorebird_flutter = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_flutter.dart'
)
shorebird_env = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_env.dart'
)
code_push_client = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_code_push_client/lib/src/code_push_client.dart'
)
code_push_protocol_gen = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_code_push_protocol/tool/gen.dart'
)
code_push_protocol_readme = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_code_push_protocol/README.md'
)
server_openapi = read_repo_file(
  repo_root,
  'shorebird-server/internal/api/handlers/openapi.yaml'
)
server_router = read_repo_file(
  repo_root,
  'shorebird-server/internal/api/handlers/router.go'
)
shorebird_cache = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/cache.dart'
)
shorebird_cli_command_runner = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_cli_command_runner.dart'
)
shorebird_documentation = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_documentation.dart'
)
doctor_command = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/commands/doctor_command.dart'
)
shorebird_process = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_process.dart'
)
shorebird_powershell_launcher = read_repo_file(repo_root, 'shorebird/bin/shorebird.ps1')
shorebird_shell_launcher = read_repo_file(
  repo_root,
  'shorebird/third_party/flutter/bin/internal/shared.sh'
)
network_checker = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/network_checker.dart'
)
shorebird_web_console = read_repo_file(
  repo_root,
  'shorebird/packages/shorebird_cli/lib/src/shorebird_web_console.dart'
)
updater_config = read_repo_file(
  repo_root,
  'updater/library/src/config.rs'
)
artifact_manifest_client = read_repo_file(
  repo_root,
  'shorebird/packages/artifact_proxy/lib/src/artifact_manifest_client.dart'
)
artifact_proxy = read_repo_file(
  repo_root,
  'shorebird/packages/artifact_proxy/lib/src/artifact_proxy.dart'
)
artifact_proxy_test = read_repo_file(
  repo_root,
  'shorebird/packages/artifact_proxy/test/artifact_proxy_test.dart'
)
artifact_proxy_config = read_repo_file(
  repo_root,
  'shorebird/packages/artifact_proxy/lib/config.dart'
)
write_gclient = read_repo_file(repo_root, 'scripts/write_gclient.sh')
free_ci_disk_linux = read_repo_file(repo_root, 'scripts/free_ci_disk_linux.sh')
check_ci_capacity = read_repo_file(repo_root, 'scripts/check_ci_capacity.sh')
verify_ci_capacity = read_repo_file(repo_root, 'scripts/verify_ci_capacity.sh')
flutter_tool_cache = read_repo_file(
  repo_root,
  'flutter/packages/flutter_tools/lib/src/cache.dart'
)
flutter_tools_pubspec = read_repo_file(
  repo_root,
  'flutter/packages/flutter_tools/pubspec.yaml'
)
flutter_http_host_validator = read_repo_file(
  repo_root,
  'flutter/packages/flutter_tools/lib/src/http_host_validator.dart'
)
flutter_gradle_constants = read_repo_file(
  repo_root,
  'flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt'
)
flutter_aar_init_script = read_repo_file(
  repo_root,
  'flutter/packages/flutter_tools/gradle/aar_init_script.gradle'
)
flutter_deps = read_repo_file(repo_root, 'flutter/DEPS')
dart_deps = read_repo_file(repo_root, 'dart-sdk/DEPS')
flutter_engine_pubspec = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/pubspec.yaml'
)
flutter_post_process_docs = read_repo_file(repo_root, 'flutter/dev/bots/post_process_docs.dart')
flutter_unpublish_package = read_repo_file(repo_root, 'flutter/dev/bots/unpublish_package.dart')
flutter_android_host_app_settings = read_repo_file(
  repo_root,
  'flutter/dev/integration_tests/pure_android_host_apps/android_host_app_v2_embedding/settings.gradle'
)
flutter_android_host_app_kts_settings = read_repo_file(
  repo_root,
  'flutter/dev/integration_tests/pure_android_host_apps/host_app_kotlin_gradle_dsl/settings.gradle.kts'
)
flutter_create_api_docs = read_repo_file(repo_root, 'flutter/dev/tools/create_api_docs.dart')
flutter_zip_bundle = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/build/zip_bundle.gni'
)
flutter_dart_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/build/dart/BUILD.gn'
)
flutter_engine_archives_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/build/archives/BUILD.gn'
)
flutter_runtime_shorebird_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/runtime/shorebird/BUILD.gn'
)
flutter_embedder_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/shell/platform/embedder/BUILD.gn'
)
flutter_linux_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/shell/platform/linux/BUILD.gn'
)
flutter_android_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/shell/platform/android/BUILD.gn'
)
flutter_web_sdk_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/web_sdk/BUILD.gn'
)
flutter_ios_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/shell/platform/darwin/ios/BUILD.gn'
)
flutter_macos_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/shell/platform/darwin/macos/BUILD.gn'
)
flutter_dart_isolate = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/runtime/dart_isolate.cc'
)
flutter_snapshot_build = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/lib/snapshot/BUILD.gn'
)
flutter_web_ui_copy_artifacts = read_repo_file(
  repo_root,
  'flutter/engine/src/flutter/lib/web_ui/dev/steps/copy_artifacts_step.dart'
)
flutter_shorebird_tests = read_repo_file(
  repo_root,
  'flutter/packages/shorebird_tests/test/shorebird_tests.dart'
)
flutter_update_dart_sdk_ps1 = read_repo_file(
  repo_root,
  'flutter/bin/internal/update_dart_sdk.ps1'
)
flutter_update_dart_sdk_sh = read_repo_file(
  repo_root,
  'flutter/bin/internal/update_dart_sdk.sh'
)
generated_artifact_manifest = IO.popen(
  [
    'python3',
    repo_path(repo_root, 'scripts/write_artifact_manifest.py'),
    '--flutter-engine-revision',
    'flutter-base-revision',
  ],
  &:read
)
assert!($?.success?, 'artifact manifest helper must run successfully')
assert!(
  generated_artifact_manifest.include?("flutter_engine_revision: 'flutter-base-revision'") &&
    generated_artifact_manifest.include?("storage_bucket: 'shorebird'") &&
    generated_artifact_manifest.include?('flutter_infra_release/flutter/$engine/android-arm64-release/artifacts.zip') &&
    generated_artifact_manifest.include?('flutter_infra_release/flutter/$engine/linux-x64-release/artifacts.zip') &&
    generated_artifact_manifest.include?('flutter_infra_release/flutter/$engine/ios-release/artifacts.zip') &&
    generated_artifact_manifest.include?('flutter_infra_release/flutter/$engine/flutter-web-sdk.zip') &&
    generated_artifact_manifest.include?('flutter_infra_release/flutter/$engine/darwin-arm64-release/FlutterMacOS.framework.zip'),
  'artifact manifest helper must emit open mirror overrides for CI-produced engine artifacts'
)
generated_manifest_yaml = YAML.safe_load(generated_artifact_manifest)
generated_artifact_overrides = generated_manifest_yaml.fetch('artifact_overrides')
missing_mirror_paths = generated_artifact_overrides.map do |artifact_path|
  "mirror/shorebird/#{artifact_path.gsub('$engine', '${engine_revision}')}"
end.reject { |mirror_path| workflow_text.include?(mirror_path) }
assert!(
  missing_mirror_paths.empty?,
  "workflow engine artifacts must package every generated mirror override: #{missing_mirror_paths.join(', ')}"
)
assert!(
  workflow_text.scan('test -f "$mirror_root').length >= generated_artifact_overrides.length,
  'workflow engine artifact packaging must verify mirror-ready files exist before uploading'
)
closed_runtime_endpoint_patterns = %w[
  download.shorebird.dev
  api.shorebird.dev
  auth.shorebird.dev
  console.shorebird.dev
  cdn.shorebird.cloud
]
runtime_endpoint_matches = Dir[
  repo_path(repo_root, 'shorebird/packages/shorebird_cli/lib/**/*.dart'),
  repo_path(repo_root, 'shorebird/packages/shorebird_code_push_client/lib/**/*.dart'),
  repo_path(repo_root, 'shorebird/packages/artifact_proxy/lib/**/*.dart'),
  repo_path(repo_root, 'updater/library/src/**/*.rs'),
  repo_path(repo_root, 'flutter/packages/flutter_tools/lib/src/cache.dart'),
  repo_path(repo_root, 'flutter/packages/flutter_tools/lib/src/http_host_validator.dart'),
  repo_path(repo_root, 'flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt'),
  repo_path(repo_root, 'flutter/packages/flutter_tools/gradle/aar_init_script.gradle'),
  repo_path(repo_root, 'flutter/DEPS'),
  repo_path(repo_root, 'flutter/dev/bots/post_process_docs.dart'),
  repo_path(repo_root, 'flutter/dev/bots/unpublish_package.dart'),
  repo_path(repo_root, 'flutter/dev/integration_tests/pure_android_host_apps/android_host_app_v2_embedding/settings.gradle'),
  repo_path(repo_root, 'flutter/dev/integration_tests/pure_android_host_apps/host_app_kotlin_gradle_dsl/settings.gradle.kts'),
  repo_path(repo_root, 'flutter/dev/tools/create_api_docs.dart'),
  repo_path(repo_root, 'flutter/bin/internal/update_dart_sdk.ps1'),
  repo_path(repo_root, 'flutter/bin/internal/update_dart_sdk.sh'),
  repo_path(repo_root, 'flutter/engine/src/flutter/build/zip_bundle.gni'),
  repo_path(repo_root, 'flutter/engine/src/flutter/lib/web_ui/dev/steps/copy_artifacts_step.dart'),
  repo_path(repo_root, 'flutter/packages/shorebird_tests/test/shorebird_tests.dart')
].flat_map do |path|
  text = File.read(path)
  closed_runtime_endpoint_patterns.each_with_object([]) do |pattern, matches|
    matches << "#{path.delete_prefix("#{repo_root}/")}: #{pattern}" if text.include?(pattern)
  end
end
assert!(
  runtime_endpoint_matches.empty?,
  "runtime code must not hard-code official hosted endpoints: #{runtime_endpoint_matches.join(', ')}"
)
official_support_link_patterns = %w[
  docs.shorebird.dev
  app.codecov.io/gh/shorebirdtech/shorebird
  codecov.io/gh/shorebirdtech/shorebird
  discord.gg/shorebird
  handbook.shorebird.dev
  producthunt.com/posts/shorebird-code-push
  shorebird.dev/privacy
  contact@shorebird.dev
  github.com/shorebirdtech/shorebird
  github.com/shorebirdtech/updater
  github.com/shorebirdtech/flutter
  git@github.com:shorebirdtech/shorebird.git
]
official_support_link_matches = Dir[
  repo_path(repo_root, 'shorebird/README.md'),
  repo_path(repo_root, 'shorebird/bin/shorebird.ps1'),
  repo_path(repo_root, 'shorebird/third_party/flutter/bin/internal/shared.sh'),
  repo_path(repo_root, 'shorebird/packages/shorebird_cli/lib/src/**/*.dart'),
  repo_path(repo_root, 'shorebird/packages/shorebird_code_push_client/lib/**/*.dart'),
  repo_path(repo_root, 'shorebird/packages/artifact_proxy/lib/**/*.dart'),
  repo_path(repo_root, 'updater/library/src/**/*.rs'),
  repo_path(repo_root, 'flutter/packages/flutter_tools/pubspec.yaml'),
  repo_path(repo_root, 'flutter/engine/src/flutter/lib/web_ui/dev/steps/copy_artifacts_step.dart')
].flat_map do |path|
  text = File.read(path)
  official_support_link_patterns.each_with_object([]) do |pattern, matches|
    matches << "#{path.delete_prefix("#{repo_root}/")}: #{pattern}" if text.include?(pattern)
  end
end
assert!(
  official_support_link_matches.empty?,
  "shorebird CLI source must direct users to open docs/issues, not official Shorebird support links: #{official_support_link_matches.join(', ')}"
)
official_package_metadata_matches = Dir[
  repo_path(repo_root, 'shorebird/packages/*/pubspec.yaml')
].flat_map do |path|
  text = File.read(path)
  [
    'github.com/shorebirdtech/shorebird',
    'homepage: https://shorebird.dev',
    'repository: https://shorebird.dev',
  ].each_with_object([]) do |pattern, matches|
    matches << "#{path.delete_prefix("#{repo_root}/")}: #{pattern}" if text.include?(pattern)
  end
end
assert!(
  official_package_metadata_matches.empty?,
  "package metadata must point at the open workspace, not official Shorebird metadata: #{official_package_metadata_matches.join(', ')}"
)
expected_submodules = {
  'dart-sdk' => ['https://github.com/tony-cloud/dart-sdk.git', 'tonycloud/dev'],
  'depot_tools' => ['https://chromium.googlesource.com/chromium/tools/depot_tools.git', 'main'],
  'flutter' => ['https://github.com/tony-cloud/flutter.git', 'tonycloud/dev'],
  'shorebird' => ['https://git.tonycloud.org/flutter/shorebird.git', 'main'],
  'shorebird-server' => ['https://git.tonycloud.org/flutter/shorebird-server.git', 'main'],
  'updater' => ['https://git.tonycloud.org/flutter/shorebird-updater.git', 'main'],
}
expected_submodules.each do |path, (url, branch)|
  assert!(
    gitmodules.include?("[submodule \"#{path}\"]") &&
      gitmodules.include?("path = #{path}") &&
      gitmodules.include?("url = #{url}") &&
      gitmodules.include?("branch = #{branch}"),
    ".gitmodules must pin open HTTPS submodule #{path} to #{url} on #{branch}"
  )
  assert!(
    repositories_doc.include?("| `#{path}` | `#{url}` | `#{branch}` |"),
    "docs/REPOSITORIES.md must document submodule #{path}"
  )
end
forbidden_gitmodule_fragments = [
  'git@',
  'file://',
  '/Users/',
  '../',
  'github.com/shorebirdtech/',
  'github.com/shorebirdtech',
]
forbidden_gitmodule_fragments.each do |fragment|
  assert!(
    !gitmodules.include?(fragment),
    ".gitmodules must not contain non-open or non-portable submodule URL fragment #{fragment}"
  )
end
assert!(
  write_gclient.include?('"url": "https://github.com/tony-cloud/dart-sdk.git"') &&
    write_gclient.include?('"url": "https://github.com/tony-cloud/flutter.git"') &&
    !write_gclient.include?('github.com/shorebirdtech'),
  'gclient generation must use the open Dart/Flutter forks and avoid official Shorebird remotes'
)
assert!(
  flutter_engine_pubspec.include?('frontend_server_client:') &&
    flutter_engine_pubspec.include?('./third_party/dart/third_party/pkg/webdev/frontend_server_client') &&
    !write_gclient.include?('"engine/src/flutter/third_party/dart/third_party/pkg/webdev": None'),
  'Flutter engine pub get needs Dart third_party/pkg/webdev for frontend_server_client, so write_gclient must not suppress it'
)
{
  'README.md' => [
    '.github/workflows/open-shorebird-ci.yml',
    'full_sdk_build=true',
    'run_runtime_smokes=true',
    'SHOREBIRD_FLUTTER_GIT_URL',
    'SHOREBIRD_FLUTTER_STORAGE_BASE_URL',
    'scripts/assemble_artifact_mirror.sh',
    'scripts/verify_upload_readiness.sh',
    'open-shorebird-artifact-mirror',
    'open-shorebird-release-manifest',
    'http://localhost:8080',
    'http://localhost:8080/artifacts',
    'http://localhost:8080/download.flutter.io',
    'DART_DYNAMIC_MODULES',
    'artifacts_manifest.yaml',
  ],
  'docs/CI.md' => [
    'custom-dart-sdk-linux-x64',
    'custom-dart-sdk-macos-arm64',
    'ios-interpreter-engine',
    'scripts/write_sha256.sh',
    'scripts/verify_upload_readiness.sh',
    'SHOREBIRD_FLUTTER_GIT_URL',
    'SHOREBIRD_FLUTTER_STORAGE_BASE_URL',
    'scripts/assemble_artifact_mirror.sh',
    'open-shorebird-artifact-mirror',
    'open-shorebird-release-manifest',
    'http://localhost:8080',
    'http://localhost:8080/artifacts',
    'http://localhost:8080/download.flutter.io',
    'http://localhost:8080/openapi.yaml',
    'mirror-metadata',
    'artifacts_manifest.yaml',
    'verify_artifact_mirror_workflow_assembly.sh',
  ],
  'docs/PLATFORM_TESTING.md' => [
    'license:pro',
    'DART_DYNAMIC_MODULES=false',
    'real iPad',
    '/openapi.yaml',
  ],
  'docs/REPOSITORIES.md' => [
    'https://github.com/tony-cloud/dart-sdk.git',
    'https://github.com/tony-cloud/flutter.git',
    'https://git.tonycloud.org/flutter/shorebird.git',
    'https://git.tonycloud.org/flutter/shorebird-server.git',
    'https://git.tonycloud.org/flutter/shorebird-updater.git',
    'Only top-level project repositories are submodules',
  ],
  'shorebird/OPEN_SOURCE_REPLACEMENTS.md' => [
    'Local forks/submodules are the source of truth',
    './scripts/sync_open_sources.sh',
    'http://localhost:8080',
    'http://localhost:8080/openapi.yaml',
    'http://localhost:8080/artifacts',
    'http://localhost:8080/download.flutter.io',
    'assemble_artifact_mirror.sh',
    'open-shorebird-artifact-mirror',
    'open-shorebird-release-manifest',
    'DART_DYNAMIC_MODULES=false',
    'verify_ci_workflow.sh',
  ],
  'shorebird/README.md' => [
    'OPEN_SOURCE_REPLACEMENTS.md',
    '.github/workflows/open-shorebird-ci.yml',
    'self-hosted services',
    'custom Dart SDK',
  ],
  'shorebird/docs/account/api-keys/README.md' => [
    'SHOREBIRD_TOKEN',
    'http://localhost:8080/auth',
  ],
  'shorebird/docs/code-push/troubleshooting/README.md' => [
    'SHOREBIRD_ARTIFACT_BASE_URL',
    'SHOREBIRD_FLUTTER_STORAGE_BASE_URL',
    'Asset changes are not part of a Dart code patch',
  ],
  'shorebird/docs/getting-started/flutter-version/README.md' => [
    'https://github.com/tony-cloud/flutter.git',
    'http://localhost:8080/artifacts',
  ],
}.each do |path, required_texts|
  text = {
    'README.md' => readme,
    'docs/CI.md' => ci_doc,
    'docs/PLATFORM_TESTING.md' => platform_doc,
    'docs/REPOSITORIES.md' => repositories_doc,
    'shorebird/README.md' => shorebird_readme,
    'shorebird/OPEN_SOURCE_REPLACEMENTS.md' => open_replacements_doc,
  }.fetch(path) { read_repo_file(repo_root, path) }
  missing_texts = required_texts.reject { |required_text| text.include?(required_text) }
  assert!(missing_texts.empty?, "#{path} missing required text: #{missing_texts.join(', ')}")
end

assert!(
  shorebird_flutter.include?('SHOREBIRD_FLUTTER_GIT_URL') &&
    shorebird_flutter.include?('defaultFlutterGitUrl') &&
    shorebird_flutter.include?('https://github.com/tony-cloud/flutter.git'),
  'shorebird Flutter installer must default to the open fork and allow SHOREBIRD_FLUTTER_GIT_URL override'
)
assert!(
  shorebird_documentation.include?('openShorebirdRepositoryUrl') &&
    shorebird_documentation.include?('https://git.tonycloud.org/flutter/shorebird') &&
    shorebird_documentation.include?(%q{openShorebirdIssueUrl = '$openShorebirdRepositoryUrl/issues/new'}) &&
    shorebird_documentation.include?(%q{docsUrl = '$openShorebirdRepositoryUrl/src/branch/main/docs'}) &&
    !shorebird_documentation.include?('docs.shorebird.dev') &&
    !shorebird_documentation.include?('github.com/shorebirdtech/shorebird'),
  'shorebird documentation links must point to the open repository docs and issue tracker'
)
{
  'shorebird_cli_command_runner.dart' => shorebird_cli_command_runner,
  'doctor_command.dart' => doctor_command,
}.each do |path, text|
  assert!(
    text.include?('https://git.tonycloud.org/flutter/shorebird.git') &&
      !text.include?('git@github.com:shorebirdtech/shorebird.git'),
    "#{path} must print the open Shorebird fork in user-visible version banners"
  )
end
assert!(
  !shorebird_flutter.include?('github.com/shorebirdtech/flutter.git'),
  'shorebird Flutter installer must not clone the closed official Shorebird Flutter fork'
)
assert!(
  shorebird_env.include?("defaultHostedUrl = 'http://localhost:8080'") &&
    shorebird_env.include?("defaultAuthServiceUrl = '\$defaultHostedUrl/auth'") &&
    shorebird_env.include?("defaultJwtIssuer = 'shorebird-auth'"),
  'shorebird environment must default to the open self-hosted server'
)
assert!(
  !shorebird_env.include?('https://auth.shorebird.dev'),
  'shorebird environment must not default to the official hosted auth service'
)
assert!(
  code_push_client.include?("defaultHostedUri = Uri.parse('http://localhost:8080')") &&
    !code_push_client.include?("Uri.https('api.shorebird.dev')"),
  'code push client must default to the open self-hosted server, not api.shorebird.dev'
)
assert!(
  code_push_protocol_gen.include?('http://localhost:8080/openapi.yaml') &&
    code_push_protocol_gen.include?('../shorebird-server/internal/api/handlers/openapi.yaml') &&
    code_push_protocol_gen.include?('Do not regenerate this package from Shorebird') &&
    !code_push_protocol_gen.include?('api.shorebird.dev') &&
    code_push_protocol_readme.include?('http://localhost:8080/openapi.yaml') &&
    code_push_protocol_readme.include?('../shorebird-server/internal/api/handlers/openapi.yaml') &&
    code_push_protocol_readme.include?("Shorebird's hosted API") &&
    !code_push_protocol_readme.include?('api.shorebird.dev'),
  'code push protocol regeneration docs must require an open/self-hosted spec, not the hosted Shorebird API'
)
assert!(
  server_openapi.include?('openapi: 3.1.0') &&
    server_openapi.include?('/openapi.yaml:') &&
    server_openapi.include?('/api/v1/openapi.yaml:') &&
    server_openapi.include?('/api/v1/patches/check:'),
  'self-hosted server must include a checked OpenAPI contract for open clients'
)
assert!(
  server_router.include?('go:embed openapi.yaml') &&
    server_router.include?('serveOpenAPISpec') &&
    server_router.include?('r.Get("/openapi.yaml", serveOpenAPISpec)') &&
    server_router.include?('r.Get("/api/v1/openapi.yaml", serveOpenAPISpec)'),
  'self-hosted server must serve the checked OpenAPI contract'
)
assert!(
  shorebird_cache.include?('SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS') &&
    shorebird_cache.include?('legacyAotToolsEnabled') &&
    shorebird_cache.include?('registerArtifact(AotToolsArtifact'),
  'cache must gate legacy aot-tools behind an explicit environment variable'
)
assert!(
  shorebird_cache.include?("defaultArtifactBaseUrl = 'http://localhost:8080/artifacts'") &&
    !shorebird_cache.include?('https://storage.googleapis.com') &&
    !shorebird_cache.include?('download.shorebird.dev'),
  'cache must default artifact downloads to the open local mirror, not Shorebird hosted storage'
)
assert!(
  shorebird_process.include?('defaultFlutterStorageBaseUrl') &&
    shorebird_process.include?('${ShorebirdEnv.defaultHostedUrl}/download.flutter.io') &&
    !shorebird_process.include?('https://download.shorebird.dev'),
  'Flutter process environment must default to the open local Flutter artifact mirror'
)
{
  'shorebird/bin/shorebird.ps1' => shorebird_powershell_launcher,
  'shorebird/third_party/flutter/bin/internal/shared.sh' => shorebird_shell_launcher,
}.each do |path, text|
  assert!(
    text.include?('SHOREBIRD_FLUTTER_GIT_URL') &&
      text.include?('https://github.com/tony-cloud/flutter.git') &&
      text.include?('SHOREBIRD_FLUTTER_STORAGE_BASE_URL') &&
      text.include?('FLUTTER_STORAGE_BASE_URL') &&
      text.include?('http://localhost:8080/download.flutter.io') &&
      !text.include?('github.com/shorebirdtech/flutter.git') &&
      !text.include?('https://download.shorebird.dev'),
    "#{path} must bootstrap from open Flutter/artifact defaults with environment overrides"
  )
end
assert!(
  shorebird_shell_launcher.include?('https://git.tonycloud.org/flutter/shorebird') &&
    !shorebird_shell_launcher.include?('github.com/shorebirdtech/shorebird'),
  'shell launcher missing-clone guidance must point at the open Shorebird repository'
)
assert!(
  network_checker.include?('shorebirdEnv.hostedUri') &&
    network_checker.include?('shorebirdEnv.authServiceUri') &&
    !network_checker.include?('api.shorebird.dev') &&
    !network_checker.include?('console.shorebird.dev') &&
    !network_checker.include?('cdn.shorebird.cloud'),
  'network checker must inspect self-hosted endpoints instead of fixed Shorebird hosted URLs'
)
assert!(
  shorebird_web_console.include?('shorebirdEnv.hostedUri') &&
    !shorebird_web_console.include?('console.shorebird.dev'),
  'web console links must use the configured self-hosted URL'
)
assert!(
  updater_config.include?('const DEFAULT_BASE_URL: &str = "http://localhost:8080";') &&
    !updater_config.include?('https://api.shorebird.dev'),
  'updater runtime must default to the open self-hosted server'
)
assert!(
  artifact_manifest_client.include?('defaultManifestBaseUri') &&
    artifact_manifest_client.include?('http://localhost:8080/artifacts') &&
    !artifact_manifest_client.include?('download.shorebird.dev'),
  'artifact manifest client must default to the open artifact mirror'
)
assert!(
  artifact_proxy.include?('defaultShorebirdArtifactBaseUri') &&
    artifact_proxy.include?('http://localhost:8080/artifacts') &&
    !artifact_proxy.include?('download.shorebird.dev') &&
    !artifact_proxy.include?('docs.shorebird.dev') &&
    !artifact_proxy.include?('shorebird.dev/contact'),
  'artifact proxy must default Shorebird-specific redirects to the open artifact mirror'
)
assert!(
  artifact_proxy_config.include?('linux-x64-release\/artifacts\.zip') &&
    artifact_proxy_config.include?('ios-release\/artifacts\.zip') &&
    artifact_proxy_config.include?('flutter-web-sdk\.zip') &&
    artifact_proxy_config.include?('flutter_patched_sdk_product\.zip') &&
    artifact_proxy_config.include?('darwin-arm64-release\/FlutterMacOS\.framework\.zip'),
  'artifact proxy must recognize the generated artifact paths produced by open CI'
)
generated_artifact_overrides.each do |artifact_path|
  expected_proxy_pattern = artifact_path
                           .gsub('/', '\\/')
                           .gsub('.', '\\.')
                           .gsub('$engine', '(.*)')
  assert!(
    artifact_proxy_config.include?(expected_proxy_pattern),
    "artifact proxy config must recognize generated override #{artifact_path}"
  )
  assert!(
    artifact_proxy_test.include?(artifact_path),
    "artifact proxy tests must cover generated override #{artifact_path}"
  )
end
assert!(
  artifact_proxy_test.include?('ios-release/artifacts.zip') &&
    artifact_proxy_test.include?('flutter-web-sdk.zip') &&
    artifact_proxy_test.include?('flutter_patched_sdk_product.zip'),
  'artifact proxy tests must cover iOS, web, and patched SDK mirror redirects'
)
assert!(
  flutter_tool_cache.include?("kOpenFlutterStorageUrl = 'http://localhost:8080/download.flutter.io'") &&
    !flutter_tool_cache.include?('download.shorebird.dev') &&
    !flutter_tool_cache.include?('kShorebirdStorageUrl'),
  'Flutter tool cache must default engine artifact downloads to the open local Flutter mirror'
)
assert!(
  flutter_http_host_validator.include?("kCloudHost = 'http://localhost:8080/download.flutter.io/'") &&
    !flutter_http_host_validator.include?('download.shorebird.dev'),
  'Flutter doctor network validator must check the open local Flutter mirror by default'
)
{
  'flutter/bin/internal/update_dart_sdk.ps1' => flutter_update_dart_sdk_ps1,
  'flutter/bin/internal/update_dart_sdk.sh' => flutter_update_dart_sdk_sh,
}.each do |path, text|
  assert!(
    text.include?('FLUTTER_STORAGE_BASE_URL') &&
      text.include?('http://localhost:8080/download.flutter.io') &&
      !text.include?('download.shorebird.dev'),
    "#{path} must download Dart SDK archives from FLUTTER_STORAGE_BASE_URL with the open local Flutter mirror fallback"
  )
end
assert!(
  flutter_gradle_constants.include?('DEFAULT_MAVEN_HOST = "http://localhost:8080/download.flutter.io"') &&
    !flutter_gradle_constants.include?('download.shorebird.dev') &&
    flutter_aar_init_script.include?('?: "http://localhost:8080/download.flutter.io"') &&
    !flutter_aar_init_script.include?('download.shorebird.dev'),
  'Flutter Gradle plugin defaults must use the open local Flutter mirror'
)
assert!(
  flutter_deps.include?('"dart_sdk_git": "https://github.com/tony-cloud/dart-sdk.git"') &&
    flutter_deps.include?('"updater_git": "https://git.tonycloud.org/flutter/shorebird-updater.git"') &&
    !flutter_deps.include?('git@github.com:shorebirdtech/dart-sdk.git') &&
    !flutter_deps.include?('github.com/shorebirdtech/updater.git') &&
    !flutter_deps.include?('shorebird-dart-sdk-prebuilt') &&
    !flutter_deps.include?('shorebirdtech/_build_engine'),
  'Flutter DEPS must point Dart/updater dependencies at open remotes and avoid Shorebird private prebuilt buckets'
)
dart_revision_ok, dart_revision = capture_command(
  'git',
  '-C',
  repo_path(repo_root, 'dart-sdk'),
  'rev-parse',
  'HEAD'
)
assert!(dart_revision_ok, 'must be able to read the Dart SDK submodule revision')
dart_tool_sdk_tag = dart_deps[/"sdk_tag": "([^"]+)"/, 1]
assert!(dart_tool_sdk_tag, 'Dart DEPS must declare sdk_tag for the bootstrap tool SDK')
assert!(
  flutter_deps.include?("\"dart_sdk_revision\": \"#{dart_revision}\"") &&
    flutter_deps.include?("'version': '#{dart_tool_sdk_tag}'"),
  'Flutter DEPS must keep its Dart source/tool SDK pins aligned with the workspace Dart SDK'
)
assert!(
  flutter_android_host_app_settings.include?('System.getenv("FLUTTER_STORAGE_BASE_URL") ?: "http://localhost:8080"') &&
    flutter_android_host_app_settings.include?('$flutterStorageUrl/download.flutter.io') &&
    !flutter_android_host_app_settings.include?('download.shorebird.dev') &&
    flutter_android_host_app_kts_settings.include?('System.getenv("FLUTTER_STORAGE_BASE_URL") ?: "http://localhost:8080"') &&
    flutter_android_host_app_kts_settings.include?('$flutterStorageUrl/download.flutter.io') &&
    !flutter_android_host_app_kts_settings.include?('download.shorebird.dev'),
  'Flutter Android host-app integration fixtures must default to the open local Flutter mirror'
)
assert!(
  flutter_create_api_docs.include?("Platform.environment['FLUTTER_STORAGE_BASE_URL']") &&
    flutter_create_api_docs.include?('http://localhost:8080/download.flutter.io') &&
    !flutter_create_api_docs.include?('download.shorebird.dev') &&
    flutter_post_process_docs.include?("Platform.environment['FLUTTER_STORAGE_BASE_URL']") &&
    flutter_post_process_docs.include?('http://localhost:8080/download.flutter.io') &&
    !flutter_post_process_docs.include?('download.shorebird.dev') &&
    flutter_unpublish_package.include?('http://localhost:8080/download.flutter.io/flutter_infra_release') &&
    !flutter_unpublish_package.include?('download.shorebird.dev') &&
    flutter_shorebird_tests.include?("'FLUTTER_STORAGE_BASE_URL': 'http://localhost:8080/download.flutter.io'") &&
    !flutter_shorebird_tests.include?('download.shorebird.dev'),
  'Flutter docs/test artifact paths must default to the open local Flutter mirror'
)
assert!(
  flutter_zip_bundle.include?('http://localhost:8080/download.flutter.io/flutter_infra_release/flutter/$engine_version/sky_engine.zip') &&
    !flutter_zip_bundle.include?('download.shorebird.dev'),
  'engine zip bundle metadata must use the open local Flutter mirror'
)
assert!(
  flutter_dart_build.include?('import("$dart_src/build/dart/copy_tree.gni")') &&
    flutter_dart_build.include?('copy_tree("copy_dart_sdk")') &&
    flutter_dart_build.include?('source = prebuilt_dart_sdk') &&
    flutter_dart_build.include?('dest = "$root_out_dir/dart-sdk"') &&
    !flutter_dart_build.include?('copy_trees('),
  'engine Dart SDK BUILD.gn must use the Dart SDK copy_tree template available in this checkout'
)
assert!(
  flutter_dart_isolate.include?('#include "flutter/shell/common/shorebird/updater.h"') &&
    flutter_dart_isolate.include?('std::string GetYamlValue(') &&
    flutter_dart_isolate.include?('return UnquoteYamlValue(GetYamlValue(config.yaml_config, key));') &&
    !flutter_dart_isolate.include?('#include "flutter/shell/common/shorebird/shorebird.h"'),
  'dart_isolate.cc must avoid depending on the full Shorebird wrapper target so GN header checks pass'
)
assert!(
  flutter_runtime_shorebird_build.include?('$dart_src/runtime/bin:shared_object_loaders') &&
    !flutter_runtime_shorebird_build.include?('$dart_src/runtime/bin:elf_loader') &&
    flutter_embedder_build.include?(%q("$dart_src/runtime/bin:common_embedder_dart_io",
      "$dart_src/runtime/bin:shared_object_loaders")) &&
    !flutter_embedder_build.include?('if (is_ios || is_mac)'),
  'Shorebird patch cache and embedder targets must depend on shared_object_loaders so GN header checks allow both ELF and Mach-O loader includes'
)
assert!(
  flutter_ios_build.include?('"//flutter/shell/common/shorebird:updater"'),
  'iOS Flutter framework source must directly depend on the Shorebird updater target because FlutterDartProject.mm includes updater.h'
)
assert!(
  flutter_web_ui_copy_artifacts.include?("io.Platform.environment['FLUTTER_STORAGE_BASE_URL']") &&
    flutter_web_ui_copy_artifacts.include?('http://localhost:8080/download.flutter.io') &&
    flutter_web_ui_copy_artifacts.include?('storageBaseUri.resolve') &&
    !flutter_web_ui_copy_artifacts.include?('download.shorebird.dev'),
  'web UI artifact downloads must use FLUTTER_STORAGE_BASE_URL with the open local Flutter mirror fallback'
)
assert!(
  flutter_tools_pubspec.include?('https://git.tonycloud.org/flutter/shorebird.git') &&
    !flutter_tools_pubspec.include?('https://github.com/shorebirdtech/shorebird.git'),
  'Flutter tools pubspec must fetch Shorebird build-trace code from the open fork'
)
assert!(
  flutter_engine_archives_build.include?('zip_bundle("artifacts")') &&
    flutter_engine_archives_build.include?('output = "$prefix/artifacts.zip"') &&
    flutter_engine_archives_build.include?('zip_bundle("flutter_patched_sdk")') &&
    flutter_engine_archives_build.include?('output = "flutter_patched_sdk${file_suffix}.zip"'),
  'engine archive BUILD.gn must still define artifacts.zip and flutter_patched_sdk_product.zip targets used by CI'
)
assert!(
  flutter_linux_build.include?('zip_bundle("flutter_gtk")') &&
    flutter_linux_build.include?('output = "${prefix}${full_target_platform_name}-flutter-gtk.zip"'),
  'Linux engine BUILD.gn must still define the flutter_gtk archive used by CI'
)
assert!(
  flutter_android_build.include?('zip_bundle("android_symbols")') &&
    flutter_android_build.include?('output = "$android_zip_archive_dir/symbols.zip"') &&
    flutter_android_build.include?('zip_bundle("flutter_jar_zip")') &&
    flutter_android_build.include?('output = "$android_zip_archive_dir/artifacts.zip"') &&
    flutter_android_build.include?('zip_bundle("gen_snapshot")') &&
    flutter_android_build.include?('zip_bundle("analyze_snapshot")'),
  'Android engine BUILD.gn must still define symbols/artifacts/gen_snapshot/analyze_snapshot archives used by CI'
)
assert!(
  flutter_web_sdk_build.include?('zip_bundle_from_file("flutter_web_sdk_archive")') &&
    flutter_web_sdk_build.include?('output = "flutter-web-sdk.zip"'),
  'web SDK BUILD.gn must still define flutter-web-sdk.zip used by CI'
)
assert!(
  flutter_ios_build.include?('action("flutter_framework")') &&
    flutter_ios_build.include?('outputs = [ "$root_out_dir/Flutter.xcframework" ]'),
  'iOS engine BUILD.gn must still define Flutter.xcframework used by CI'
)
assert!(
  flutter_macos_build.include?('zip_bundle("zip_macos_flutter_framework")') &&
    flutter_macos_build.include?('output = "${prefix}FlutterMacOS.framework.zip"'),
  'macOS engine BUILD.gn must still define FlutterMacOS.framework.zip used by CI'
)
assert!(
  flutter_snapshot_build.include?('action("create_macos_gen_snapshots")') &&
    flutter_snapshot_build.include?('action("create_macos_analyze_snapshots")'),
  'snapshot BUILD.gn must still define macOS/iOS host snapshotter targets used by CI'
)
assert!(
  !shorebird_cache.include?('aot-tools.dill artifact.\n/// Used for linking and generating optimized AOT snapshots.'),
  'cache comments must not describe legacy closed aot-tools as a default optimized AOT path'
)
platform_test_common = read_repo_file(repo_root, 'scripts/platform_test_common.sh')
bootstrap_linux = read_repo_file(repo_root, 'scripts/bootstrap_linux.sh')
bootstrap_macos = read_repo_file(repo_root, 'scripts/bootstrap_macos.sh')
sync_open_sources = read_repo_file(repo_root, 'scripts/sync_open_sources.sh')
sync_flutter_prebuilt_dart_sdk = read_repo_file(
  repo_root,
  'scripts/sync_flutter_prebuilt_dart_sdk.sh'
)
verify_sync_open_sources = read_repo_file(
  repo_root,
  'scripts/verify_sync_open_sources.sh'
)
verify_open_infrastructure_defaults = read_repo_file(
  repo_root,
  'scripts/verify_open_infrastructure_defaults.sh'
)
verify_powershell_open_defaults = read_repo_file(
  repo_root,
  'scripts/verify_powershell_open_defaults.sh'
)
android_runtime_smoke = read_repo_file(repo_root, 'scripts/android_runtime_patch_smoke.sh')
verify_ios_interpreter_route = read_repo_file(
  repo_root,
  'scripts/verify_ios_interpreter_route.sh'
)
verify_ios_interpreter_route_validator = read_repo_file(
  repo_root,
  'scripts/verify_ios_interpreter_route_validator.sh'
)
verify_engine_args = read_repo_file(repo_root, 'scripts/verify_engine_args.sh')
verify_dart_sdk_args = read_repo_file(repo_root, 'scripts/verify_dart_sdk_args.sh')
verify_dart_tool_sdk = read_repo_file(repo_root, 'scripts/verify_dart_tool_sdk.sh')
assert!(
  bootstrap_linux.include?('exec "$ROOT/scripts/platform_test_common.sh" linux') &&
    bootstrap_macos.include?('exec "$ROOT/scripts/platform_test_common.sh" macos'),
  'platform bootstrap entry points must delegate to the shared open replacement test script'
)
assert!(
  platform_test_common.include?("cd '$ROOT/shorebird' && '$DART_BIN' pub get") &&
    platform_test_common.include?('test/src/shorebird_env_test.dart') &&
    platform_test_common.include?('test/src/commands/doctor_command_test.dart') &&
    platform_test_common.include?('packages/shorebird_code_push_client/test/src/code_push_client_test.dart') &&
    platform_test_common.include?('packages/artifact_proxy/test/artifact_proxy_test.dart') &&
    platform_test_common.include?('packages/artifact_proxy/test/server_bin_test.dart') &&
    platform_test_common.include?('CARGO_BIN="${CARGO_BIN:-cargo}"') &&
    platform_test_common.include?('--manifest-path "$ROOT/updater/library/Cargo.toml"'),
  'platform bootstrap must test CLI, code push client, artifact proxy, and updater open server defaults'
)
assert!(
  step_uses(jobs.fetch('source-checks'), 'dtolnay/rust-toolchain@stable'),
  'source-checks must install Rust so updater runtime tests run in default CI'
)
source_bootstrap_step = jobs.fetch('source-checks').fetch('steps', []).find do |step|
  step['run'].to_s.include?('./scripts/bootstrap_linux.sh')
end
assert!(
  source_bootstrap_step && source_bootstrap_step.dig('env', 'SKIP_GCLIENT_SYNC') == '1',
  'source-checks must run bootstrap_linux.sh with SKIP_GCLIENT_SYNC=1 for source-level CI'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('python3 -m py_compile scripts/write_artifact_manifest.py'),
  'source-checks must compile the artifact manifest helper'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('python3 -m py_compile scripts/validate_artifact_mirror.py'),
  'source-checks must compile the artifact mirror validator'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('python3 -m py_compile scripts/validate_release_manifest.py'),
  'source-checks must compile the release manifest validator'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('python3 -m py_compile scripts/write_release_manifest.py'),
  'source-checks must compile the release manifest helper'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_ci_capacity.sh'),
  'source-checks must smoke-test heavy runner capacity checks'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_sync_open_sources.sh'),
  'source-checks must smoke-test source sync behavior in an isolated workspace'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_open_infrastructure_defaults.sh'),
  'source-checks must verify open infrastructure defaults independently of the workflow contract'
)
assert!(
  verify_open_infrastructure_defaults.include?('BUILD_SENSITIVE_FILES') &&
    verify_open_infrastructure_defaults.include?('check_forbidden_in_tree "$ROOT/shorebird/packages/artifact_proxy/lib" dart') &&
    verify_open_infrastructure_defaults.include?('https://github.com/tony-cloud/dart-sdk.git') &&
    verify_open_infrastructure_defaults.include?('https://git.tonycloud.org/flutter/shorebird-updater.git') &&
    verify_open_infrastructure_defaults.include?('http://localhost:8080/download.flutter.io') &&
    verify_open_infrastructure_defaults.include?('http://localhost:8080/artifacts') &&
    verify_open_infrastructure_defaults.include?('defaultHostedUrl = ') &&
    verify_open_infrastructure_defaults.include?('download.shorebird.dev') &&
    verify_open_infrastructure_defaults.include?('shorebird-dart-sdk-prebuilt'),
  'open infrastructure defaults verifier must scan build-sensitive files and enforce open remotes/artifact hosts'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_powershell_open_defaults.sh'),
  'source-checks must verify Windows PowerShell launcher/updater open defaults'
)
assert!(
  verify_powershell_open_defaults.include?('shorebird/bin/shorebird.ps1') &&
    verify_powershell_open_defaults.include?('flutter/bin/internal/update_dart_sdk.ps1') &&
    verify_powershell_open_defaults.include?('System.Management.Automation.Language.Parser') &&
    verify_powershell_open_defaults.include?('https://github.com/tony-cloud/flutter.git') &&
    verify_powershell_open_defaults.include?('http://localhost:8080/download.flutter.io') &&
    verify_powershell_open_defaults.include?('download.shorebird.dev') &&
    verify_powershell_open_defaults.include?('github.com/shorebirdtech/flutter.git'),
  'PowerShell verifier must parse Windows scripts and enforce open Flutter/artifact defaults'
)
assert!(
  verify_sync_open_sources.include?('expected forbidden Dart SDK remote to fail') &&
    verify_sync_open_sources.include?('expected forbidden updater source remote to fail') &&
    verify_sync_open_sources.include?('expected forbidden explicit UPDATER_URL to fail'),
  'source sync smoke test must reject upstream Dart SDK and official Shorebird updater remotes'
)
assert!(
  verify_dart_tool_sdk.include?('pkg/front_end/pubspec.yaml') &&
    verify_dart_tool_sdk.include?('Dart tool SDK version does not satisfy front_end SDK constraint') &&
    verify_dart_tool_sdk.include?('Flutter engine Dart checkout points at'),
  'Dart tool SDK verifier must reject stale bootstrap SDKs and broken Flutter engine Dart links'
)
assert!(
  sync_flutter_prebuilt_dart_sdk.include?('dart-sdk/tools/sdks/dart-sdk') &&
    sync_flutter_prebuilt_dart_sdk.include?('flutter/engine/src/flutter/prebuilts/$HOST_CONFIG/dart-sdk') &&
    sync_flutter_prebuilt_dart_sdk.include?('bin/dartaotruntime') &&
    sync_flutter_prebuilt_dart_sdk.include?('bin/snapshots/kernel_worker_aot.dart.snapshot') &&
    sync_flutter_prebuilt_dart_sdk.include?('ln -s') &&
    !sync_flutter_prebuilt_dart_sdk.include?('shorebird-dart-sdk-prebuilt'),
  'Flutter web prebuilt Dart SDK sync must link the open Dart tool SDK into Flutter prebuilts'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_release_manifest.sh'),
  'source-checks must smoke-test release manifest validation'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_artifact_mirror_validator.sh'),
  'source-checks must smoke-test standalone artifact mirror validation'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_assemble_artifact_mirror.sh'),
  'source-checks must smoke-test assembling downloaded CI artifacts into a publishable mirror'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_artifact_mirror_workflow_assembly.sh'),
  'source-checks must smoke-test the artifact-mirror aggregation job with fake downloaded artifacts'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_hosted_full_sdk_build.sh --help >/dev/null 2>&1'),
  'source-checks must smoke-test hosted full-SDK helper argument parsing without contacting GitHub'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('./scripts/verify_ios_interpreter_route_validator.sh'),
  'source-checks must smoke-test iOS interpreter route artifact validation'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('bash -n scripts/*.sh shorebird/third_party/flutter/bin/internal/shared.sh flutter/bin/internal/update_dart_sdk.sh'),
  'source-checks must syntax-check the patched Flutter launchers/updaters as well as repo scripts'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?('python3 -m py_compile scripts/safe_extract_tar.py'),
  'source-checks must syntax-check the shared safe tar extractor'
)
assert!(
  job_runs(jobs.fetch('source-checks')).join("\n").include?("compile(Path('flutter/DEPS').read_text") &&
    job_runs(jobs.fetch('source-checks')).join("\n").include?("'flutter/DEPS', 'exec'"),
  'source-checks must syntax-check Flutter DEPS before heavy gclient sync'
)
assemble_artifact_mirror = read_repo_file(repo_root, 'scripts/assemble_artifact_mirror.sh')
validate_artifact_mirror = read_repo_file(repo_root, 'scripts/validate_artifact_mirror.py')
validate_release_manifest = read_repo_file(repo_root, 'scripts/validate_release_manifest.py')
safe_extract_tar = read_repo_file(repo_root, 'scripts/safe_extract_tar.py')
verify_assemble_artifact_mirror = read_repo_file(
  repo_root,
  'scripts/verify_assemble_artifact_mirror.sh'
)
verify_artifact_mirror_validator = read_repo_file(
  repo_root,
  'scripts/verify_artifact_mirror_validator.sh'
)
verify_artifact_mirror_workflow_assembly = read_repo_file(
  repo_root,
  'scripts/verify_artifact_mirror_workflow_assembly.sh'
)
verify_downloaded_release_artifacts = read_repo_file(
  repo_root,
  'scripts/verify_downloaded_release_artifacts.sh'
)
verify_hosted_full_sdk_build = read_repo_file(
  repo_root,
  'scripts/verify_hosted_full_sdk_build.sh'
)
verify_upload_readiness = read_repo_file(repo_root, 'scripts/verify_upload_readiness.sh')
write_release_manifest = read_repo_file(repo_root, 'scripts/write_release_manifest.py')
verify_release_manifest = read_repo_file(repo_root, 'scripts/verify_release_manifest.sh')
assert!(
  assemble_artifact_mirror.include?('scan_for_shorebird_trees "$INPUT_DIR"') &&
    assemble_artifact_mirror.include?("find \"$INPUT_DIR\" -type f \\( -name '*.tar.gz' -o -name '*.tgz' \\)") &&
    assemble_artifact_mirror.include?('scripts/safe_extract_tar.py') &&
    assemble_artifact_mirror.include?('conflicting mirror file') &&
    assemble_artifact_mirror.include?('validate_artifact_mirror.py') &&
    assemble_artifact_mirror.include?('write_sha256.sh'),
  'artifact mirror assembler must safely extract archives, merge direct and archived shorebird/ subtrees, reject conflicts, write checksum sidecars, and run the shared mirror validator'
)
assert!(
  safe_extract_tar.include?('unsafe archive member path') &&
    safe_extract_tar.include?('unsupported archive member type') &&
    safe_extract_tar.include?('duplicate archive member path') &&
    safe_extract_tar.include?('archive member escapes extraction root') &&
    safe_extract_tar.include?('tarfile.open') &&
    safe_extract_tar.include?('extract_safe_tar_archive'),
  'shared safe tar extractor must reject unsafe paths, unsupported members, duplicate file entries, and extraction escapes'
)
assert!(
  validate_artifact_mirror.include?('REQUIRED_PATCH_ZIPS') &&
    validate_artifact_mirror.include?('"patch-linux-x64.zip": "patch"') &&
    validate_artifact_mirror.include?('"patch-darwin-x64.zip": "patch"') &&
    validate_artifact_mirror.include?('"patch-darwin-arm64.zip": "patch"') &&
    validate_artifact_mirror.include?('"patch-windows-x64.zip": "patch.exe"') &&
    validate_artifact_mirror.include?('validate_sidecars') &&
    validate_artifact_mirror.include?('validate_manifest_overrides') &&
    validate_artifact_mirror.include?('validate_patch_zips') &&
    validate_artifact_mirror.include?('is_safe_relative_path') &&
    validate_artifact_mirror.include?('is_plain_file') &&
    validate_artifact_mirror.include?('unsafe artifact override path') &&
    validate_artifact_mirror.include?('artifact override is empty') &&
    validate_artifact_mirror.include?('symlink entries are not allowed') &&
    validate_artifact_mirror.include?('replace("$engine", engine_revision)') &&
    validate_artifact_mirror.include?('zipfile.ZipFile(zip_path)') &&
    validate_artifact_mirror.include?('names != [expected_entry]'),
  'artifact mirror validator must validate sidecars, manifest overrides, and patch-tool ZIP contracts'
)
assert!(
  verify_assemble_artifact_mirror.include?('linux-engine-x64.tar.gz') &&
    verify_assemble_artifact_mirror.include?('patch-linux-x64.zip') &&
    verify_assemble_artifact_mirror.include?('patch-darwin-x64.zip') &&
    verify_assemble_artifact_mirror.include?('patch-darwin-arm64.zip') &&
    verify_assemble_artifact_mirror.include?('patch-windows-x64.zip') &&
    verify_assemble_artifact_mirror.include?('artifacts_manifest.yaml') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed a conflicting mirror file') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed an unsafe tar member') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed a tar symlink member') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed a duplicate tar member') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed an invalid patch zip') &&
    verify_assemble_artifact_mirror.include?('unexpectedly allowed a missing manifest override'),
  'artifact mirror assembler smoke test must cover archived engine subtrees, patch artifacts, metadata, conflicts, unsafe tar members, duplicate tar members, invalid patch zips, and missing overrides'
)
assert!(
  verify_artifact_mirror_validator.include?('validate_artifact_mirror.py') &&
    verify_artifact_mirror_validator.include?('unexpectedly accepted a stale sidecar') &&
    verify_artifact_mirror_validator.include?('unexpectedly accepted an unsafe manifest override') &&
    verify_artifact_mirror_validator.include?('unexpectedly accepted a symlink artifact') &&
    verify_artifact_mirror_validator.include?('unexpectedly accepted an empty manifest override artifact') &&
    verify_artifact_mirror_validator.include?('verify_assemble_artifact_mirror.sh'),
  'artifact mirror validator smoke test must cover valid mirrors, unsafe override paths, empty override artifacts, symlink artifacts, and stale sidecar rejection'
)
assert!(
  verify_artifact_mirror_workflow_assembly.include?('mirror-input') &&
    verify_artifact_mirror_workflow_assembly.include?('open-shorebird-artifact-mirror.tar.gz') &&
    verify_artifact_mirror_workflow_assembly.include?('open-shorebird-release-manifest.json') &&
    verify_artifact_mirror_workflow_assembly.include?('write_release_manifest.py') &&
    verify_artifact_mirror_workflow_assembly.include?('validate_release_manifest.py') &&
    verify_artifact_mirror_workflow_assembly.include?('verify_downloaded_release_artifacts.sh') &&
    verify_artifact_mirror_workflow_assembly.include?('validate_artifact_mirror.py') &&
    verify_artifact_mirror_workflow_assembly.include?('safe_extract_tar.py') &&
    verify_artifact_mirror_workflow_assembly.include?('mirror-metadata/*artifacts_manifest.yaml') &&
    verify_artifact_mirror_workflow_assembly.include?('flutter_patched_sdk_product.zip') &&
    verify_artifact_mirror_workflow_assembly.include?('unexpectedly accepted downloaded artifacts for the wrong github_sha') &&
    verify_artifact_mirror_workflow_assembly.include?('unexpectedly accepted a stale downloaded release manifest sidecar') &&
    verify_artifact_mirror_workflow_assembly.include?('unexpectedly accepted a stale downloaded mirror archive sidecar') &&
    verify_artifact_mirror_workflow_assembly.include?('unexpectedly accepted an unsafe downloaded mirror archive'),
  'artifact-mirror workflow dry-run must exercise mirror-only input assembly, release manifest requirements, downloaded sidecar rejection, and unsafe downloaded mirror archive rejection'
)
assert!(
  verify_downloaded_release_artifacts.include?('open-shorebird-release-manifest.json') &&
    verify_downloaded_release_artifacts.include?('open-shorebird-artifact-mirror.tar.gz') &&
    verify_downloaded_release_artifacts.include?('validate_release_manifest.py') &&
    verify_downloaded_release_artifacts.include?('validate_artifact_mirror.py') &&
    verify_downloaded_release_artifacts.include?('--github-sha') &&
    verify_downloaded_release_artifacts.include?('EXPECTED_GITHUB_SHA') &&
    verify_downloaded_release_artifacts.include?('validate_manifest_args') &&
    verify_downloaded_release_artifacts.include?('verify_sha256_sidecar') &&
    verify_downloaded_release_artifacts.include?('scripts/safe_extract_tar.py') &&
    verify_downloaded_release_artifacts.include?('manifest_sidecar') &&
    verify_downloaded_release_artifacts.include?('mirror_sidecar') &&
    verify_downloaded_release_artifacts.include?('digest mismatch') &&
    verify_downloaded_release_artifacts.include?('filename mismatch') &&
    verify_downloaded_release_artifacts.include?('expected exactly one') &&
    verify_downloaded_release_artifacts.include?('mirror archive did not contain open-shorebird-artifact-mirror/'),
  'downloaded release verifier must validate manifest and mirror checksum sidecars plus safely extracted mirror archive from downloaded GitHub artifacts'
)
assert!(
  verify_hosted_full_sdk_build.include?('gh workflow run "$WORKFLOW"') &&
    verify_hosted_full_sdk_build.include?('-f full_sdk_build=true') &&
    verify_hosted_full_sdk_build.include?('-f run_runtime_smokes=false') &&
    verify_hosted_full_sdk_build.include?('gh run download "$run_id"') &&
    verify_hosted_full_sdk_build.include?('GITHUB_TOKEN or GH_TOKEN is required') &&
    verify_hosted_full_sdk_build.include?('api_request POST "/actions/workflows/$WORKFLOW/dispatches"') &&
    verify_hosted_full_sdk_build.include?('archive_download_url') &&
    verify_hosted_full_sdk_build.include?('unzip -q "$zip_path" -d "$artifact_dir"') &&
    verify_hosted_full_sdk_build.include?('--json status,conclusion,url,headSha') &&
    verify_hosted_full_sdk_build.include?('--github-sha "$run_head_sha"') &&
    verify_hosted_full_sdk_build.include?('unable to read headSha') &&
    verify_hosted_full_sdk_build.include?('verify_downloaded_release_artifacts.sh') &&
    verify_hosted_full_sdk_build.include?('--linux-heavy-runner') &&
    verify_hosted_full_sdk_build.include?('--macos-heavy-runner') &&
    verify_hosted_full_sdk_build.include?('--skip-gclient-sync'),
  'hosted full SDK verifier must dispatch the manual full build, download artifacts, and run the release artifact verifier'
)
verify_ci_workflow_shell = read_repo_file(repo_root, 'scripts/verify_ci_workflow.sh')
assert!(
  verify_ci_workflow_shell.include?('--require-tracked') &&
    verify_ci_workflow_shell.include?('--require-clean') &&
    verify_ci_workflow_shell.include?('--require-upload-ready') &&
    verify_ci_workflow_shell.include?('RUBY_ARGS'),
  'workflow verifier shell wrapper must expose tracked, clean, and upload-ready modes'
)
assert!(
  verify_upload_readiness.include?('verify_ci_workflow.sh') &&
    verify_upload_readiness.include?('--require-upload-ready') &&
    verify_upload_readiness.include?('upload readiness check passed'),
  'upload readiness verifier must run the workflow contract in upload-ready mode'
)
assert!(
    write_release_manifest.include?('artifact_count') &&
    write_release_manifest.include?('github_sha') &&
    write_release_manifest.include?('"artifact_group": artifact_relative.parts[0]') &&
    write_release_manifest.include?('"filename": artifact_path.name') &&
    write_release_manifest.include?('fnmatch.fnmatchcase') &&
    write_release_manifest.include?('missing required artifact matching') &&
    write_release_manifest.include?('is_safe_relative_path') &&
    write_release_manifest.include?('is_plain_file') &&
    write_release_manifest.include?('unsafe artifact path') &&
    write_release_manifest.include?('empty artifacts are not allowed') &&
    write_release_manifest.include?('symlink artifacts are not allowed') &&
    write_release_manifest.include?('parse_sidecar') &&
    write_release_manifest.include?('digest mismatch') &&
    write_release_manifest.include?('filename mismatch') &&
    write_release_manifest.include?('orphan .sha256 sidecars'),
  'release manifest helper must validate checksum sidecars and write artifact provenance'
)
assert!(
    validate_release_manifest.include?('format_version') &&
    validate_release_manifest.include?('artifact_count') &&
    validate_release_manifest.include?('github_sha is') &&
    validate_release_manifest.include?('artifact_group must be a string') &&
    validate_release_manifest.include?('does not match path group') &&
    validate_release_manifest.include?('filename must be a string') &&
    validate_release_manifest.include?('does not match path filename') &&
    validate_release_manifest.include?('is_safe_relative_path') &&
    validate_release_manifest.include?('is_plain_file') &&
    validate_release_manifest.include?('PurePosixPath') &&
    validate_release_manifest.include?('unsafe artifact path') &&
    validate_release_manifest.include?('duplicate artifact path') &&
    validate_release_manifest.include?('missing artifact file') &&
    validate_release_manifest.include?('empty artifacts are not allowed') &&
    validate_release_manifest.include?('size mismatch') &&
    validate_release_manifest.include?('digest mismatch') &&
    validate_release_manifest.include?('sidecar digest mismatch') &&
    validate_release_manifest.include?('artifacts missing from release manifest') &&
    validate_release_manifest.include?('sidecars missing from release manifest'),
  'release manifest validator must verify manifest structure, digests, sizes, sidecars, and complete artifact coverage'
)
assert!(
  verify_release_manifest.include?('open-shorebird-cli-linux-x64.tar.gz') &&
    verify_release_manifest.include?('shorebird-server-linux-amd64.tar.gz') &&
    verify_release_manifest.include?('validate_release_manifest.py') &&
    verify_release_manifest.include?('unexpectedly accepted a tampered digest') &&
    verify_release_manifest.include?('unexpectedly accepted the wrong github_sha') &&
    verify_release_manifest.include?('unexpectedly accepted an unlisted artifact') &&
    verify_release_manifest.include?('unexpectedly accepted an unsafe artifact path') &&
    verify_release_manifest.include?('unexpectedly accepted bad provenance fields') &&
    verify_release_manifest.include?('unexpectedly accepted a symlink artifact') &&
    verify_release_manifest.include?('unexpectedly accepted an empty artifact') &&
    verify_release_manifest.include?('unexpectedly accepted a missing sidecar') &&
    verify_release_manifest.include?('unexpectedly accepted a bad sidecar') &&
    verify_release_manifest.include?('unexpectedly accepted an orphan sidecar') &&
    verify_release_manifest.include?('unexpectedly accepted a missing required artifact'),
  'release manifest smoke test must cover valid artifacts, required globs, provenance fields, path safety, symlink rejection, and checksum failure modes'
)
assert!(
  sync_open_sources.include?('real_path()') &&
    sync_open_sources.include?('ensure_source_link "$DART_TARGET" "$DART_SRC" "Dart SDK"') &&
    sync_open_sources.include?('reject_forbidden_remotes') &&
    sync_open_sources.include?('github.com/dart-lang/sdk') &&
    sync_open_sources.include?('dart.googlesource.com/sdk') &&
    sync_open_sources.include?('github.com/shorebirdtech/updater') &&
    sync_open_sources.include?('target symlink points at') &&
    sync_open_sources.include?('UPDATER_URL="${UPDATER_URL:-}"') &&
    sync_open_sources.include?('ensure_source_link "$TARGET" "$UPDATER_SRC" "updater submodule"') &&
    sync_open_sources.include?('link_checkout "$target" "$source" "$label"'),
  'open source sync must force engine Dart/updater dependencies to workspace submodules and reject known unpatched/official remotes'
)
assert!(
  !sync_open_sources.include?('github.com/shorebirdtech/updater.git'),
  'open source sync must not default to the official Shorebird updater repository'
)
assert!(
  !open_replacements_doc.include?('github.com/shorebirdtech/updater.git') &&
    !open_replacements_doc.include?('sync_open_sources.ps1') &&
    !open_replacements_doc.include?('public `shorebirdtech/updater`'),
  'open replacement audit must document local submodules, not official upstream clone instructions'
)

required_jobs = %w[
  source-checks
  cli-artifacts
  server-artifacts
  custom-dart-sdk
  custom-dart-sdk-macos
  linux-engine
  android-engine
  web-sdk
  ios-engine
  artifact-mirror
  linux-runtime-smoke
  android-runtime-smoke
]
missing_jobs = required_jobs.reject { |job_name| jobs.key?(job_name) }
assert!(missing_jobs.empty?, "missing jobs: #{missing_jobs.join(', ')}")

(required_jobs - ['source-checks']).each do |job_name|
  assert!(
    Array(jobs.fetch(job_name).fetch('needs', [])).include?('source-checks'),
    "#{job_name} must depend on source-checks before producing artifacts or running runtime smokes"
  )
end

on_config = workflow['on'] || workflow[true] || {}
inputs = on_config.dig('workflow_dispatch', 'inputs') || {}
assert!(
  workflow.dig('defaults', 'run', 'shell') == 'bash',
  'workflow must default run steps to bash for cross-platform script portability'
)
required_inputs = %w[
  full_sdk_build
  run_gclient_sync
  base_flutter_engine_revision
  linux_heavy_runner
  macos_heavy_runner
  sdk_min_free_disk_gb
  engine_min_free_disk_gb
  run_runtime_smokes
  linux_runtime_runner
  android_runtime_runner
  android_serial
]
missing_inputs = required_inputs.reject { |input| inputs.key?(input) }
assert!(missing_inputs.empty?, "missing workflow_dispatch inputs: #{missing_inputs.join(', ')}")
assert!(
  workflow.dig('env', 'BASE_FLUTTER_ENGINE_REVISION').to_s.include?('base_flutter_engine_revision'),
  'workflow must expose base_flutter_engine_revision as BASE_FLUTTER_ENGINE_REVISION'
)
assert!(
  workflow.dig('env', 'SDK_MIN_FREE_DISK_GB').to_s.include?('sdk_min_free_disk_gb') &&
    workflow.dig('env', 'ENGINE_MIN_FREE_DISK_GB').to_s.include?('engine_min_free_disk_gb'),
  'workflow must expose dispatch-configurable heavy job disk thresholds'
)
assert!(
  workflow.dig('env', 'DEPOT_TOOLS_UPDATE').to_s == '0' &&
    platform_test_common.include?('DEPOT_TOOLS_UPDATE="${DEPOT_TOOLS_UPDATE:-0}"'),
  'workflow and bootstrap must keep depot_tools pinned unless explicitly overridden'
)

%w[source-checks cli-artifacts server-artifacts].each do |job_name|
  condition = jobs.fetch(job_name).fetch('if', '')
  assert!(
    !condition.to_s.include?('full_sdk_build'),
    "#{job_name} must run by default, not only during full_sdk_build"
  )
end

%w[custom-dart-sdk custom-dart-sdk-macos linux-engine android-engine web-sdk ios-engine].each do |job_name|
  condition = jobs.fetch(job_name).fetch('if', '').to_s
  assert!(
    condition.include?("github.event_name != 'workflow_dispatch'") &&
      condition.include?('inputs.full_sdk_build'),
    "#{job_name} must run on default push/PR CI and allow manual full_sdk_build opt-out"
  )
  run_text = job_runs(jobs.fetch(job_name)).join("\n")
  assert!(
    run_text.include?('python3 --version') &&
      run_text.include?('gclient help >/dev/null') &&
      run_text.include?('ninja --version'),
    "#{job_name} must verify depot_tools-provided build tools before heavy builds"
  )
  gclient_sync_steps = jobs.fetch(job_name).fetch('steps', []).select do |step|
    step.fetch('name', '').start_with?('gclient sync')
  end
  assert!(
    !gclient_sync_steps.empty?,
    "#{job_name} must run gclient sync before heavy builds"
  )
  gclient_sync_steps.each do |step|
    condition = step.fetch('if', '').to_s
    assert!(
      condition.include?("github.event_name != 'workflow_dispatch'") &&
        condition.include?('inputs.run_gclient_sync'),
      "#{job_name} #{step.fetch('name')} must run on push/PR CI and allow manual run_gclient_sync opt-out"
    )
  end
end
{
  'custom-dart-sdk' => 'SDK_MIN_FREE_DISK_GB',
  'custom-dart-sdk-macos' => 'SDK_MIN_FREE_DISK_GB',
  'linux-engine' => 'ENGINE_MIN_FREE_DISK_GB',
  'android-engine' => 'ENGINE_MIN_FREE_DISK_GB',
  'web-sdk' => 'ENGINE_MIN_FREE_DISK_GB',
  'ios-engine' => 'ENGINE_MIN_FREE_DISK_GB',
}.each do |job_name, disk_env_var|
  run_text = job_runs(jobs.fetch(job_name)).join("\n")
  assert!(
    run_text.include?('scripts/check_ci_capacity.sh') &&
      run_text.include?("CI_MIN_FREE_DISK_GB=\"$#{disk_env_var}\""),
    "#{job_name} must fail early on runners with insufficient free disk"
  )
end
artifact_mirror_condition = jobs.fetch('artifact-mirror').fetch('if', '').to_s
assert!(
  artifact_mirror_condition.include?("github.event_name != 'workflow_dispatch'") &&
    artifact_mirror_condition.include?('full_sdk_build'),
  'artifact-mirror must run on default push/PR CI and allow manual full_sdk_build opt-out'
)
%w[cli-artifacts linux-engine android-engine web-sdk ios-engine].each do |dependency|
  assert!(
    Array(jobs.fetch('artifact-mirror').fetch('needs', [])).include?(dependency),
    "artifact-mirror must depend on #{dependency}"
  )
end
%w[server-artifacts custom-dart-sdk custom-dart-sdk-macos].each do |dependency|
  assert!(
    Array(jobs.fetch('artifact-mirror').fetch('needs', [])).include?(dependency),
    "artifact-mirror must depend on #{dependency} so release manifest covers it"
  )
end

%w[linux-runtime-smoke android-runtime-smoke].each do |job_name|
  condition = jobs.fetch(job_name).fetch('if', '').to_s
  assert!(
    condition.include?('workflow_dispatch') && condition.include?('run_runtime_smokes'),
    "#{job_name} must be gated by manual run_runtime_smokes dispatch"
  )
  assert!(
    !condition.include?('full_sdk_build'),
    "#{job_name} must not require full_sdk_build"
  )
end

cli_matrix = matrix_include(jobs.fetch('cli-artifacts'))
expected_cli_targets = [
  ['ubuntu-latest', 'linux', 'x64', 'patch-linux-x64.zip', 'patch'],
  ['macos-15-intel', 'macos', 'x64', 'patch-darwin-x64.zip', 'patch'],
  ['macos-14', 'macos', 'arm64', 'patch-darwin-arm64.zip', 'patch'],
  ['windows-latest', 'windows', 'x64', 'patch-windows-x64.zip', 'patch.exe'],
]
expected_cli_targets.each do |runner_os, artifact_os, artifact_arch, patch_zip, patch_entry|
  assert!(
    cli_matrix.any? do |entry|
      entry['os'] == runner_os &&
        entry['artifact_os'] == artifact_os &&
        entry['artifact_arch'] == artifact_arch &&
        entry['patch_zip'] == patch_zip &&
        entry['patch_entry'] == patch_entry
    end,
    "missing CLI target #{artifact_os}/#{artifact_arch} on #{runner_os} with #{patch_zip}"
  )
end
assert!(
  upload_names(jobs.fetch('cli-artifacts')).include?('cli-${{ matrix.artifact_os }}-${{ matrix.artifact_arch }}'),
  'CLI artifacts must be uploaded'
)
assert!(
  upload_names(jobs.fetch('cli-artifacts')).include?('mirror-${{ matrix.patch_zip }}'),
  'mirror patch artifacts must be uploaded'
)
assert!(
  upload_names(jobs.fetch('cli-artifacts')).include?('mirror-metadata'),
  'mirror artifact metadata must be uploaded'
)
assert!(
  step_uses(jobs.fetch('cli-artifacts'), 'dtolnay/rust-toolchain@stable'),
  'CLI artifact job must install a stable Rust toolchain'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('cargo build --release --manifest-path updater/patch/Cargo.toml --bin patch'),
  'CLI artifact job must build the public updater patch binary'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('names = archive.namelist()') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('names != [expected_entry]') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('info.file_size <= 0') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('${{ matrix.patch_entry }}'),
  'CLI artifact job must verify patch mirror ZIPs contain the exact patch entry expected by the cache'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('mirror_patch_dir="artifacts/mirror/shorebird/$engine_revision"') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('cp "artifacts/mirror/${{ matrix.patch_zip }}" "$mirror_patch_dir/${{ matrix.patch_zip }}"') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('test -f "$mirror_patch_dir/${{ matrix.patch_zip }}"') &&
    jobs.fetch('cli-artifacts').fetch('steps').any? do |step|
      step.dig('with', 'path').to_s.include?('artifacts/mirror/shorebird/**/${{ matrix.patch_zip }}') &&
        step.dig('with', 'path').to_s.include?('artifacts/mirror/shorebird/**/${{ matrix.patch_zip }}.sha256')
    end,
  'CLI artifact job must upload a publish-ready shorebird/<engine>/ patch mirror subtree'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('scripts/write_artifact_manifest.py') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('artifacts_manifest.yaml') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('artifacts/mirror/shorebird/$engine_revision') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('base_engine_revision="${BASE_FLUTTER_ENGINE_REVISION:-$engine_revision}"') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('--flutter-engine-revision "$base_engine_revision"'),
  'CLI artifact job must generate open artifact mirror metadata'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('packages/artifact_proxy/bin/server.dart') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('bin/artifact_proxy${{ matrix.extension }}') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('artifact_proxy${{ matrix.extension }} --health-check'),
  'CLI artifact job must compile, package, and smoke-test the open artifact proxy binary'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?("grep -q 'https://git.tonycloud.org/flutter/shorebird.git' <<<\"$version_output\"") &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?("grep -q 'https://git.tonycloud.org/flutter/shorebird.git' <<<\"$extracted_version_output\"") &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?("git@github.com:shorebirdtech/shorebird.git") &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('compiled CLI still reports the official Shorebird SSH remote') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('extracted CLI still reports the official Shorebird SSH remote'),
  'CLI artifact job must assert compiled and extracted version banners report the open fork'
)
assert!(
  job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('cli_extract_dir="$(mktemp -d)"') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('tar -C "$cli_extract_dir" -xzf open-shorebird-cli-${{ matrix.artifact_os }}-${{ matrix.artifact_arch }}.tar.gz') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('test -f "$cli_extract_dir/manifest.json"') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('"$cli_extract_dir/bin/open_aot_patch_tools${{ matrix.extension }}" --help') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('"$cli_extract_dir/bin/shorebird${{ matrix.extension }}" --version') &&
    job_runs(jobs.fetch('cli-artifacts')).join("\n").include?('"$cli_extract_dir/bin/artifact_proxy${{ matrix.extension }}" --health-check'),
  'CLI artifact job must extract and smoke-test the downloadable archive layout'
)

server_matrix = matrix_include(jobs.fetch('server-artifacts'))
expected_server_targets = [
  ['linux', 'amd64'],
  ['linux', 'arm64'],
  ['darwin', 'amd64'],
  ['darwin', 'arm64'],
  ['windows', 'amd64'],
]
expected_server_targets.each do |goos, goarch|
  assert!(
    server_matrix.any? { |entry| entry['goos'] == goos && entry['goarch'] == goarch },
    "missing server target #{goos}/#{goarch}"
  )
end
assert!(
  upload_names(jobs.fetch('server-artifacts')).include?('shorebird-server-${{ matrix.goos }}-${{ matrix.goarch }}'),
  'server artifacts must be uploaded'
)
assert!(
  inputs.dig('full_sdk_build', 'default') == true &&
    inputs.dig('linux_heavy_runner', 'default') == 'ubuntu-latest' &&
    inputs.dig('macos_heavy_runner', 'default') == 'macos-latest' &&
    inputs.dig('sdk_min_free_disk_gb', 'default') == 8 &&
    inputs.dig('engine_min_free_disk_gb', 'default') == 8,
  'heavy SDK/engine workflow inputs must default to managed GitHub-hosted runners and enabled full builds'
)
assert!(
  jobs.fetch('custom-dart-sdk').fetch('runs-on').to_s.include?("inputs.linux_heavy_runner || 'ubuntu-latest'") &&
    jobs.fetch('android-engine').fetch('runs-on').to_s.include?("inputs.linux_heavy_runner || 'ubuntu-latest'") &&
    jobs.fetch('web-sdk').fetch('runs-on').to_s.include?("inputs.linux_heavy_runner || 'ubuntu-latest'") &&
    jobs.fetch('custom-dart-sdk-macos').fetch('runs-on').to_s.include?("inputs.macos_heavy_runner || 'macos-latest'") &&
    jobs.fetch('ios-engine').fetch('runs-on').to_s.include?("inputs.macos_heavy_runner || 'macos-latest'"),
  'heavy SDK/engine jobs must default to managed GitHub-hosted runners'
)
assert!(
  workflow.dig('env', 'JAVA_VERSION').to_s == '17',
  'workflow must pin the Java version used by Android builds'
)
assert!(
  free_ci_disk_linux.include?('GITHUB_ACTIONS') &&
    free_ci_disk_linux.include?('RUNNER_ENVIRONMENT:-github-hosted') &&
    free_ci_disk_linux.include?('CI_FREE_DISK_SPACE_FORCE') &&
    free_ci_disk_linux.include?('CI_FREE_DISK_SPACE:-1') &&
    free_ci_disk_linux.include?('sudo rm -rf "$path"'),
  'Linux disk cleanup must be explicitly gated for GitHub-hosted runners and overridable on custom runners'
)
assert!(
  check_ci_capacity.include?('CI_MIN_FREE_DISK_GB') &&
    check_ci_capacity.include?('CI_CAPACITY_PATH') &&
    check_ci_capacity.include?('CI_AVAILABLE_DISK_KB_OVERRIDE') &&
    check_ci_capacity.include?('df -Pk "$CHECK_PATH"') &&
    check_ci_capacity.include?('runner has insufficient free disk'),
  'heavy runner capacity check must validate free disk with portable df output and a test override'
)
assert!(
  verify_ci_capacity.include?('CI_AVAILABLE_DISK_KB_OVERRIDE') &&
    verify_ci_capacity.include?('unexpectedly accepted insufficient disk') &&
    verify_ci_capacity.include?('unexpectedly accepted an invalid minimum') &&
    verify_ci_capacity.include?('CI_MIN_FREE_DISK_GB=0'),
  'capacity check smoke test must cover pass, fail, invalid input, and disabled modes'
)
assert!(
  step_uses(jobs.fetch('android-engine'), 'actions/setup-java@v4') &&
    workflow_text.include?('distribution: temurin') &&
    workflow_text.include?('java-version: ${{ env.JAVA_VERSION }}') &&
    job_runs(jobs.fetch('android-runtime-smoke')).join("\n").include?('java -version') &&
    android_runtime_smoke.include?('require_tool java'),
  'Android engine and runtime smoke paths must provision or verify Java explicitly'
)
server_artifact_run_text = job_runs(jobs.fetch('server-artifacts')).join("\n")
assert!(
  server_artifact_run_text.include?('GOOS" == "linux"') &&
    server_artifact_run_text.include?('GOARCH" == "amd64"') &&
    server_artifact_run_text.include?('cp internal/api/handlers/openapi.yaml') &&
    server_artifact_run_text.include?('"openapi.yaml"') &&
    server_artifact_run_text.include?('curl -fsS http://127.0.0.1:18080/health') &&
    server_artifact_run_text.include?('grep -q \'"status":"ok"\'') &&
    server_artifact_run_text.include?('grep -q \'<!DOCTYPE html>\'') &&
    server_artifact_run_text.include?('curl -fsS http://127.0.0.1:18080/openapi.yaml') &&
    server_artifact_run_text.include?("grep -q 'openapi: 3.1.0'"),
  'server artifact job must package and smoke-test the native Linux package from its archive'
)

required_uploads = {
  'custom-dart-sdk' => ['custom-dart-sdk-linux-x64'],
  'custom-dart-sdk-macos' => ['custom-dart-sdk-macos-arm64'],
  'linux-engine' => ['linux-engine-x64'],
  'android-engine' => ['android-engine-arm64'],
  'web-sdk' => ['flutter-web-sdk'],
  'ios-engine' => ['ios-interpreter-engine', 'macos-engine-arm64'],
  'artifact-mirror' => ['open-shorebird-artifact-mirror', 'open-shorebird-release-manifest'],
}
required_uploads.each do |job_name, expected_names|
  actual_names = upload_names(jobs.fetch(job_name))
  expected_names.each do |expected_name|
    assert!(actual_names.include?(expected_name), "#{job_name} must upload #{expected_name}")
  end
end

jobs.each do |job_name, job|
  job.fetch('steps', []).each do |step|
    next unless step['uses'].to_s.start_with?('actions/upload-artifact@')

    assert!(
      step.dig('with', 'if-no-files-found') == 'error',
      "#{job_name} upload #{step.dig('with', 'name')} must fail on missing files"
    )
    assert!(
      step.dig('with', 'path').to_s.include?('.sha256'),
      "#{job_name} upload #{step.dig('with', 'name')} must include a checksum sidecar"
    )
  end
end

run_text_by_job = jobs.transform_values { |job| job_runs(job).join("\n") }
assert!(
  run_text_by_job.fetch('cli-artifacts').include?('"flutter_revision": "${flutter_revision}"') &&
    run_text_by_job.fetch('cli-artifacts').include?('"engine_revision": "${engine_revision}"'),
  'CLI artifact manifest must record Flutter and engine revisions'
)
assert!(
  run_text_by_job.fetch('server-artifacts').include?('server_revision="$(git rev-parse HEAD)"') &&
    run_text_by_job.fetch('server-artifacts').include?('"server_git_sha": "${server_revision}"'),
  'server artifact manifest must record the server source revision'
)
%w[custom-dart-sdk custom-dart-sdk-macos].each do |job_name|
  assert!(
    run_text_by_job.fetch(job_name).include?('dart_sdk_revision="$(git -C dart-sdk rev-parse HEAD)"') &&
      run_text_by_job.fetch(job_name).include?('"dart_sdk_git_sha": "${dart_sdk_revision}"'),
    "#{job_name} manifest must record the Dart SDK source revision"
  )
end
{
  'custom-dart-sdk' => 'custom-dart-sdk-linux-x64',
  'custom-dart-sdk-macos' => 'custom-dart-sdk-macos-arm64',
}.each do |job_name, artifact_name|
  run_text = run_text_by_job.fetch(job_name)
  assert!(
    run_text.include?('sdk_extract_dir="$(mktemp -d)"') &&
      run_text.include?("tar -C \"$sdk_extract_dir\" -xzf #{artifact_name}.tar.gz") &&
      run_text.include?("test -f \"$sdk_extract_dir/#{artifact_name}/manifest.json\"") &&
      run_text.include?("test -f \"$sdk_extract_dir/#{artifact_name}/args.gn\"") &&
      run_text.include?("test -x \"$sdk_extract_dir/#{artifact_name}/dart-sdk/bin/dart\"") &&
      run_text.include?("test -x \"$sdk_extract_dir/#{artifact_name}/gen_snapshot\"") &&
      run_text.include?("test -x \"$sdk_extract_dir/#{artifact_name}/dartaotruntime\"") &&
      run_text.include?("\"$sdk_extract_dir/#{artifact_name}/dart-sdk/bin/dart\" --version"),
    "#{job_name} must extract and smoke-test the downloadable Dart SDK archive layout"
  )
end
%w[linux-engine android-engine web-sdk ios-engine].each do |job_name|
  assert!(
    run_text_by_job.fetch(job_name).include?('engine_revision="$(cat flutter/bin/internal/engine.version)"') &&
      run_text_by_job.fetch(job_name).include?('"engine_revision": "${engine_revision}"'),
    "#{job_name} manifest must record the Flutter engine revision"
  )
end
engine_archive_checks = {
  'linux-engine' => [
    'engine_extract_dir="$(mktemp -d)"',
    'tar -C "$engine_extract_dir" -xzf linux-engine-x64.tar.gz',
    'test -f "$engine_extract_dir/linux-engine/manifest.json"',
    'test -f "$engine_extract_dir/linux-engine/linux_release_x64.args.gn"',
    'test -f "$engine_extract_dir/linux-engine/linux-x64-flutter-gtk.zip"',
    'test -f "$engine_extract_dir/linux-engine/flutter_patched_sdk_product.zip"',
    'test -f "$engine_extract_dir/linux-engine/artifacts.zip"',
    'test -f "$engine_extract_dir/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/linux-x64-release/linux-x64-flutter-gtk.zip"',
    'test -f "$engine_extract_dir/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/linux-x64-release/artifacts.zip"',
    'test -f "$engine_extract_dir/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/flutter_patched_sdk_product.zip"',
  ],
  'android-engine' => [
    'engine_extract_dir="$(mktemp -d)"',
    'tar -C "$engine_extract_dir" -xzf android-engine-arm64.tar.gz',
    'test -f "$engine_extract_dir/android-engine/manifest.json"',
    'test -f "$engine_extract_dir/android-engine/android_release_arm64.args.gn"',
    'test -f "$engine_extract_dir/android-engine/artifacts.zip"',
    'test -f "$engine_extract_dir/android-engine/symbols.zip"',
    'test -f "$engine_extract_dir/android-engine/flutter.jar"',
    'test -f "$engine_extract_dir/android-engine/libflutter.so"',
    'test -f "$engine_extract_dir/android-engine/gen_snapshot_arm64"',
    'test -f "$engine_extract_dir/android-engine/analyze_snapshot_arm64"',
    'test -f "$engine_extract_dir/android-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/android-arm64-release/artifacts.zip"',
    'test -f "$engine_extract_dir/android-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/android-arm64-release/symbols.zip"',
  ],
  'web-sdk' => [
    'sdk_extract_dir="$(mktemp -d)"',
    'tar -C "$sdk_extract_dir" -xzf flutter-web-sdk.tar.gz',
    'test -f "$sdk_extract_dir/web-sdk/manifest.json"',
    'test -f "$sdk_extract_dir/web-sdk/wasm_release.args.gn"',
    'test -f "$sdk_extract_dir/web-sdk/flutter-web-sdk.zip"',
    'test -f "$sdk_extract_dir/web-sdk/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/flutter-web-sdk.zip"',
  ],
  'ios-engine' => [
    'engine_extract_dir="$(mktemp -d)"',
    'tar -C "$engine_extract_dir" -xzf ios-interpreter-engine.tar.gz',
    'test -f "$engine_extract_dir/ios-engine/manifest.json"',
    'test -d "$engine_extract_dir/ios-engine/Flutter.framework"',
    'test -d "$engine_extract_dir/ios-engine/Flutter.xcframework"',
    'test -f "$engine_extract_dir/ios-engine/ios-release/artifacts.zip"',
    'test -f "$engine_extract_dir/ios-engine/ios_release.args.gn"',
    'test -f "$engine_extract_dir/ios-engine/host_release_arm64.args.gn"',
    'test -x "$engine_extract_dir/ios-engine/host_release_arm64/gen_snapshot"',
    'test -f "$engine_extract_dir/ios-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/ios-release/artifacts.zip"',
    '"Flutter.xcframework/Info.plist"',
    '"Flutter.xcframework/ios-arm64/Flutter.framework/Flutter"',
    '"gen_snapshot_arm64"',
    '"analyze_snapshot_arm64"',
    '"entitlements.txt"',
    '"without_entitlements.txt"',
    '"unsigned_binaries.txt"',
    'tar -C "$engine_extract_dir" -xzf macos-engine-arm64.tar.gz',
    'test -f "$engine_extract_dir/macos-engine/manifest.json"',
    'test -f "$engine_extract_dir/macos-engine/macos_release_arm64.args.gn"',
    'test -f "$engine_extract_dir/macos-engine/FlutterMacOS.framework.zip"',
    'test -f "$engine_extract_dir/macos-engine/flutter_patched_sdk_product.zip"',
    'test -f "$engine_extract_dir/macos-engine/mirror/shorebird/flutter_infra_release/flutter/$engine_revision/darwin-arm64-release/FlutterMacOS.framework.zip"',
  ],
}
engine_archive_checks.each do |job_name, expected_texts|
  run_text = run_text_by_job.fetch(job_name)
  missing_texts = expected_texts.reject { |expected_text| run_text.include?(expected_text) }
  assert!(
    missing_texts.empty?,
    "#{job_name} must extract and smoke-test downloadable engine archive layout: #{missing_texts.join(', ')}"
  )
end
expected_outputs = {
  'cli-artifacts' => [
    'open-shorebird-cli-${{ matrix.artifact_os }}-${{ matrix.artifact_arch }}.tar.gz',
    'artifacts/mirror/${{ matrix.patch_zip }}',
    'artifacts_manifest.yaml',
  ],
  'server-artifacts' => [
    'shorebird-server-${GOOS}-${GOARCH}.tar.gz',
  ],
  'custom-dart-sdk' => [
    'custom-dart-sdk-linux-x64.tar.gz',
  ],
  'custom-dart-sdk-macos' => [
    'custom-dart-sdk-macos-arm64.tar.gz',
  ],
  'linux-engine' => [
    'linux-engine-x64.tar.gz',
  ],
  'android-engine' => [
    'android-engine-arm64.tar.gz',
  ],
  'web-sdk' => [
    'flutter-web-sdk.tar.gz',
  ],
  'ios-engine' => [
    'ios-interpreter-engine.tar.gz',
    'macos-engine-arm64.tar.gz',
  ],
  'artifact-mirror' => [
    'open-shorebird-artifact-mirror.tar.gz',
    'open-shorebird-release-manifest.json',
  ],
}
expected_outputs.each do |job_name, outputs|
  run_text = run_text_by_job.fetch(job_name)
  outputs.each do |output|
    assert!(run_text.include?(output), "#{job_name} must create #{output}")
    assert!(
      run_text.include?("#{output}.sha256"),
      "#{job_name} must create checksum sidecar for #{output}"
    )
  end
end
%w[
  cli-artifacts
  server-artifacts
  custom-dart-sdk
  custom-dart-sdk-macos
  linux-engine
  android-engine
  web-sdk
  ios-engine
  artifact-mirror
].each do |job_name|
  assert!(
    run_text_by_job.fetch(job_name).include?('write_sha256.sh'),
    "#{job_name} must use scripts/write_sha256.sh for checksum sidecars"
  )
end
assert!(
  run_text_by_job.fetch('custom-dart-sdk').include?('verify_dart_sdk_args.sh dart-sdk/out/ReleaseX64/args.gn'),
  'Linux Dart SDK job must verify patched SDK args'
)
assert!(
  run_text_by_job.fetch('custom-dart-sdk-macos').include?('verify_dart_sdk_args.sh dart-sdk/xcodebuild/ReleaseARM64/args.gn'),
  'macOS Dart SDK job must verify patched SDK args'
)
%w[custom-dart-sdk custom-dart-sdk-macos].each do |job_name|
  run_text = run_text_by_job.fetch(job_name)
  assert!(run_text.include?('dart_dynamic_modules=false'), "#{job_name} must disable DDM")
  assert!(run_text.include?('dart_enable_aot_patching=true'), "#{job_name} must enable AOT patching")
  assert!(run_text.include?('dart_enable_shorebird_interpreter=true'), "#{job_name} must enable the interpreter route")
  assert!(run_text.include?('DartAPI_AotPatchingConfiguration'), "#{job_name} must run AOT patching VM tests")
  assert!(run_text.include?('DartAPI_BytecodePatchReloadConfiguration'), "#{job_name} must run bytecode reload VM tests")
end

assert!(
  run_text_by_job.fetch('ios-engine').include?('--shorebird-interpreter') &&
    run_text_by_job.fetch('ios-engine').include?('rustup target add aarch64-apple-ios aarch64-apple-darwin') &&
    run_text_by_job.fetch('ios-engine').include?('--no-prebuilt-dart-sdk') &&
    run_text_by_job.fetch('ios-engine').include?("--gn-args='dart_dynamic_modules=false dart_enable_aot_patching=true dart_enable_shorebird_interpreter=true shorebird_use_interpreter=true flutter_prebuilt_dart_sdk=false'") &&
    run_text_by_job.fetch('ios-engine').include?("--gn-args='flutter_prebuilt_dart_sdk=false'") &&
    run_text_by_job.fetch('ios-engine').include?('verify_ios_interpreter_route.sh') &&
    run_text_by_job.fetch('ios-engine').include?('test -x flutter/engine/src/out/host_release_arm64/gen_snapshot') &&
    run_text_by_job.fetch('ios-engine').include?('host_release_arm64/gen_snapshot') &&
    run_text_by_job.fetch('ios-engine').include?('ios-release/artifacts.zip') &&
    run_text_by_job.fetch('ios-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/ios-release/artifacts.zip') &&
    run_text_by_job.fetch('ios-engine').include?('"dart_enable_aot_patching": true') &&
    run_text_by_job.fetch('ios-engine').include?('"dart_enable_shorebird_interpreter": true') &&
    run_text_by_job.fetch('ios-engine').include?('"shorebird_enable_aot_patching": false') &&
    run_text_by_job.fetch('ios-engine').include?('"shorebird_use_interpreter": true'),
  'Apple engine job must build, verify, package, and describe the iOS interpreter host snapshotter'
)
assert!(
  verify_ios_interpreter_route.include?('json.load(file)') &&
    verify_ios_interpreter_route.include?('metadata = artifact.get("metadata")') &&
    verify_ios_interpreter_route.scan('require_gn_value "$args_file" dart_enable_aot_patching true').length >= 2 &&
    verify_ios_interpreter_route.scan('require_gn_value "$args_file" flutter_prebuilt_dart_sdk false').length >= 2 &&
    verify_ios_interpreter_route.include?('sub("[[:space:]]*$", "", value)') &&
    verify_ios_interpreter_route.include?('require(metadata, "runtime_mode", "dart-bytecode-interpreter", "metadata")') &&
    verify_ios_interpreter_route.include?('require(metadata, "target_os", "ios", "metadata")') &&
    verify_ios_interpreter_route.include?('require(metadata, "target_arch", "arm64", "metadata")') &&
    verify_ios_interpreter_route.include?('require(artifact, "payload_kind", "full-snapshot", "artifact")') &&
    verify_ios_interpreter_route.include?('require(encryption, "algorithm", "AES-256-GCM", "encryption")') &&
    verify_ios_interpreter_route.include?('require_base64(artifact, "encrypted_payload_base64", "artifact")') &&
    verify_ios_interpreter_route.include?('payload_sha256') &&
    verify_ios_interpreter_route.include?('aad_sha256'),
  'iOS route gate must parse and validate encrypted interpreter artifact metadata, not grep raw strings'
)
assert!(
  verify_ios_interpreter_route_validator.include?('write_artifact "$valid_artifact" "dart-bytecode-interpreter"') &&
    verify_ios_interpreter_route_validator.scan('dart_enable_aot_patching = true').length >= 2 &&
    verify_ios_interpreter_route_validator.include?('dart_enable_aot_patching = false') &&
    verify_ios_interpreter_route_validator.include?('shorebird_use_interpreter = false') &&
    verify_ios_interpreter_route_validator.scan('flutter_prebuilt_dart_sdk = false').length >= 2 &&
    verify_ios_interpreter_route_validator.include?('write_artifact "$bad_runtime" "dart-dynamic-modules"') &&
    verify_ios_interpreter_route_validator.include?('write_artifact "$bad_target" "dart-bytecode-interpreter" "android"') &&
    verify_ios_interpreter_route_validator.include?('unexpectedly accepted malformed JSON') &&
    verify_ios_interpreter_route_validator.include?('unexpectedly accepted a Mach-O patch artifact'),
  'iOS route validator smoke test must cover valid interpreter artifacts and invalid native/DDM/malformed cases'
)
assert!(
  [verify_engine_args, verify_dart_sdk_args, verify_ios_interpreter_route].all? do |script|
    script.include?('read_gn_value()') &&
      script.include?('sub("[[:space:]]*$", "", value)') &&
      script.include?('print value')
  end,
  'GN arg verifiers must read the final assignment so default and explicit args match GN semantics'
)
assert!(
  run_text_by_job.fetch('linux-engine').include?('verify_engine_args.sh') &&
    run_text_by_job.fetch('linux-engine').include?('verify_dart_tool_sdk.sh') &&
    run_text_by_job.fetch('linux-engine').include?('--no-prebuilt-dart-sdk') &&
    run_text_by_job.fetch('linux-engine').include?('flutter/engine/src/out/linux_release_x64/args.gn') &&
    run_text_by_job.fetch('linux-engine').include?('dart_enable_aot_patching=true') &&
    run_text_by_job.fetch('linux-engine').include?('dart_enable_shorebird_interpreter=false') &&
    run_text_by_job.fetch('linux-engine').include?('shorebird_enable_aot_patching=true') &&
    run_text_by_job.fetch('linux-engine').include?('shorebird_use_interpreter=false') &&
    run_text_by_job.fetch('linux-engine').include?('flutter_prebuilt_dart_sdk=false') &&
    run_text_by_job.fetch('linux-engine').include?('linux-x64-flutter-gtk.zip') &&
    run_text_by_job.fetch('linux-engine').include?('flutter_patched_sdk_product.zip') &&
    run_text_by_job.fetch('linux-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/linux-x64-release/artifacts.zip') &&
    run_text_by_job.fetch('linux-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/flutter_patched_sdk_product.zip'),
  'Linux engine job must build and verify the native AOT patch runtime and package GTK/patched SDK artifacts'
)
assert!(
  run_text_by_job.fetch('android-engine').include?('verify_engine_args.sh') &&
    run_text_by_job.fetch('android-engine').include?('verify_dart_tool_sdk.sh') &&
    run_text_by_job.fetch('android-engine').include?('rustup target add aarch64-linux-android') &&
    run_text_by_job.fetch('android-engine').include?('--no-prebuilt-dart-sdk') &&
    run_text_by_job.fetch('android-engine').include?('flutter/engine/src/out/android_release_arm64/args.gn') &&
    run_text_by_job.fetch('android-engine').include?('dart_enable_aot_patching=true') &&
    run_text_by_job.fetch('android-engine').include?('dart_enable_shorebird_interpreter=false') &&
    run_text_by_job.fetch('android-engine').include?('shorebird_enable_aot_patching=true') &&
    run_text_by_job.fetch('android-engine').include?('shorebird_use_interpreter=false') &&
    run_text_by_job.fetch('android-engine').include?('flutter_prebuilt_dart_sdk=false') &&
    run_text_by_job.fetch('android-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/android-arm64-release/artifacts.zip') &&
    run_text_by_job.fetch('android-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/android-arm64-release/symbols.zip'),
  'Android engine job must build and verify the native AOT patch runtime without DDM or interpreter mode'
)
assert!(
  run_text_by_job.fetch('web-sdk').include?('verify_engine_args.sh') &&
    run_text_by_job.fetch('web-sdk').include?('verify_dart_tool_sdk.sh') &&
    run_text_by_job.fetch('web-sdk').include?('sync_flutter_prebuilt_dart_sdk.sh linux-x64') &&
    run_text_by_job.fetch('web-sdk').include?('flutter/engine/src/out/wasm_release/args.gn') &&
    run_text_by_job.fetch('web-sdk').include?('dart_dynamic_modules=false') &&
    run_text_by_job.fetch('web-sdk').include?('flutter_prebuilt_dart_sdk=true') &&
    run_text_by_job.fetch('web-sdk').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/flutter-web-sdk.zip'),
  'web SDK job must explicitly disable and verify DDM'
)
assert!(
  run_text_by_job.fetch('ios-engine').include?('verify_engine_args.sh') &&
    run_text_by_job.fetch('ios-engine').include?('verify_dart_tool_sdk.sh') &&
    run_text_by_job.fetch('ios-engine').include?('--no-prebuilt-dart-sdk') &&
    run_text_by_job.fetch('ios-engine').include?('flutter/engine/src/out/macos_release_arm64/args.gn') &&
    run_text_by_job.fetch('ios-engine').include?('dart_enable_aot_patching=true') &&
    run_text_by_job.fetch('ios-engine').include?('dart_enable_shorebird_interpreter=false') &&
    run_text_by_job.fetch('ios-engine').include?('shorebird_enable_aot_patching=true') &&
    run_text_by_job.fetch('ios-engine').include?('shorebird_use_interpreter=false') &&
    run_text_by_job.fetch('ios-engine').include?('flutter_prebuilt_dart_sdk=false') &&
    run_text_by_job.fetch('ios-engine').include?('FlutterMacOS.framework.zip') &&
    run_text_by_job.fetch('ios-engine').include?('flutter_patched_sdk_product.zip') &&
    run_text_by_job.fetch('ios-engine').include?('mirror/shorebird/flutter_infra_release/flutter/${engine_revision}/darwin-arm64-release/FlutterMacOS.framework.zip'),
  'macOS engine artifact must build and verify the native AOT patch runtime without DDM or interpreter mode, while leaving the shared patched SDK mirror path to linux-engine'
)
assert!(
  run_text_by_job.fetch('linux-runtime-smoke').include?('test -d flutter/engine/src/out/linux_release_x64') &&
    run_text_by_job.fetch('linux-runtime-smoke').include?('scripts/linux_runtime_patch_smoke.sh'),
  'Linux runtime smoke job must require linux_release_x64 and run scripts/linux_runtime_patch_smoke.sh'
)
assert!(
  run_text_by_job.fetch('android-runtime-smoke').include?('scripts/android_runtime_patch_smoke.sh'),
  'Android runtime smoke job must run scripts/android_runtime_patch_smoke.sh'
)

artifact_mirror_run_text = run_text_by_job.fetch('artifact-mirror')
assert!(
  jobs.fetch('artifact-mirror').fetch('steps').count do |step|
    step['uses'].to_s == 'actions/download-artifact@v4'
  end >= 6,
  'artifact-mirror must download mirror patch/metadata and engine artifact producers'
)
%w[
  mirror-*
  cli-*
  shorebird-server-*
  custom-dart-sdk-linux-x64
  custom-dart-sdk-macos-arm64
  linux-engine-x64
  android-engine-arm64
  flutter-web-sdk
  ios-interpreter-engine
  macos-engine-arm64
].each do |download_name|
  assert!(
    jobs.fetch('artifact-mirror').fetch('steps').any? do |step|
      step['uses'].to_s == 'actions/download-artifact@v4' &&
        step.fetch('with', {}).values.any? { |value| value.to_s.include?(download_name) }
    end,
    "artifact-mirror must download #{download_name}"
  )
end
assert!(
  artifact_mirror_run_text.include?('./scripts/assemble_artifact_mirror.sh') &&
    artifact_mirror_run_text.include?('downloaded-artifacts') &&
    artifact_mirror_run_text.include?('mkdir -p mirror-input') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/mirror-* mirror-input/') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/linux-engine-x64 mirror-input/') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/android-engine-arm64 mirror-input/') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/flutter-web-sdk mirror-input/') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/ios-interpreter-engine mirror-input/') &&
    artifact_mirror_run_text.include?('cp -R downloaded-artifacts/macos-engine-arm64 mirror-input/') &&
    artifact_mirror_run_text.match?(
      %r{\./scripts/assemble_artifact_mirror\.sh\s+\\\n\s+mirror-input\s+\\\n\s+artifacts/open-shorebird-artifact-mirror}
    ) &&
    artifact_mirror_run_text.include?('artifacts/open-shorebird-artifact-mirror') &&
    artifact_mirror_run_text.include?('tar -C artifacts -czf open-shorebird-artifact-mirror.tar.gz open-shorebird-artifact-mirror') &&
    artifact_mirror_run_text.include?('python3 scripts/safe_extract_tar.py open-shorebird-artifact-mirror.tar.gz "$mirror_extract_dir"') &&
    artifact_mirror_run_text.include?('open-shorebird-artifact-mirror.tar.gz.sha256') &&
    artifact_mirror_run_text.include?('validate_artifact_mirror.py "$mirror_extract_dir/open-shorebird-artifact-mirror"') &&
    artifact_mirror_run_text.include?('scripts/write_release_manifest.py') &&
    artifact_mirror_run_text.include?('scripts/validate_release_manifest.py') &&
    artifact_mirror_run_text.include?('"$manifest_input"') &&
    artifact_mirror_run_text.include?('open-shorebird-release-manifest.json') &&
    artifact_mirror_run_text.include?('open-shorebird-release-manifest.json.sha256') &&
    artifact_mirror_run_text.include?('--github-sha "${{ github.sha }}"'),
  'artifact-mirror must assemble, archive, checksum, and manifest a publishable mirror root'
)
%w[
  cli-linux-x64/*open-shorebird-cli-linux-x64.tar.gz
  cli-macos-x64/*open-shorebird-cli-macos-x64.tar.gz
  cli-macos-arm64/*open-shorebird-cli-macos-arm64.tar.gz
  cli-windows-x64/*open-shorebird-cli-windows-x64.tar.gz
  shorebird-server-linux-amd64/*shorebird-server-linux-amd64.tar.gz
  shorebird-server-linux-arm64/*shorebird-server-linux-arm64.tar.gz
  shorebird-server-darwin-amd64/*shorebird-server-darwin-amd64.tar.gz
  shorebird-server-darwin-arm64/*shorebird-server-darwin-arm64.tar.gz
  shorebird-server-windows-amd64/*shorebird-server-windows-amd64.tar.gz
  custom-dart-sdk-linux-x64/*custom-dart-sdk-linux-x64.tar.gz
  custom-dart-sdk-macos-arm64/*custom-dart-sdk-macos-arm64.tar.gz
  linux-engine-x64/*linux-engine-x64.tar.gz
  android-engine-arm64/*android-engine-arm64.tar.gz
  flutter-web-sdk/*flutter-web-sdk.tar.gz
  ios-interpreter-engine/*ios-interpreter-engine.tar.gz
  macos-engine-arm64/*macos-engine-arm64.tar.gz
  mirror-patch-linux-x64.zip/*patch-linux-x64.zip
  mirror-patch-darwin-x64.zip/*patch-darwin-x64.zip
  mirror-patch-darwin-arm64.zip/*patch-darwin-arm64.zip
  mirror-patch-windows-x64.zip/*patch-windows-x64.zip
  mirror-metadata/*artifacts_manifest.yaml
  open-shorebird-artifact-mirror/*open-shorebird-artifact-mirror.tar.gz
].each do |required_glob|
  assert!(
    artifact_mirror_run_text.include?("--require '#{required_glob}'"),
    "artifact-mirror release manifest must require #{required_glob}"
  )
end
%w[
  artifacts_manifest.yaml
  patch-linux-x64.zip
  patch-darwin-x64.zip
  patch-darwin-arm64.zip
  patch-windows-x64.zip
  android-arm64-release/artifacts.zip
  android-arm64-release/symbols.zip
  linux-x64-release/artifacts.zip
  linux-x64-release/linux-x64-flutter-gtk.zip
  ios-release/artifacts.zip
  flutter-web-sdk.zip
  darwin-arm64-release/FlutterMacOS.framework.zip
  flutter_patched_sdk_product.zip
].each do |required_path|
  assert!(
    artifact_mirror_run_text.include?(required_path),
    "artifact-mirror archive smoke must verify #{required_path}"
  )
end

assert!(
  !workflow_text.match?(/\bdart_dynamic_modules\s*=\s*true\b/),
  'workflow must not enable DART_DYNAMIC_MODULES'
)
assert!(
  !workflow_text.include?('aot-tools.dill'),
  'workflow must not publish the legacy closed/native AOT tools artifact'
)
assert!(
  !workflow_text.include?('shasum'),
  'workflow must use scripts/write_sha256.sh instead of runner-specific shasum'
)
assert!(
  workflow_text.include?('scripts/write_sha256.sh'),
  'workflow must use the workspace checksum helper'
)

puts "workflow yaml ok: #{workflow_path}"
puts "workflow run blocks ok: #{scripts.length} checked"
puts "workflow artifact contract ok"
