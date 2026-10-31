// One released native fixture for BOTH B and U. Patch creation changes only
// Dart; the real CLI archive comparison must continue to enforce this boundary.
#include "FullFfiFixture.h"

#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stddef.h>

int64_t IosFullFfiVersion(void) { return 2; }
int64_t IosFullFfiScalarV1(int64_t value) { return value + 100; }
int64_t IosFullFfiScalarV2(int64_t value) { return value + 200; }
IosFullFfiRecordV1 IosFullFfiChangeRecordV1(IosFullFfiRecordV1 value) {
  ++value.number; value.fraction *= 2; return value;
}
IosFullFfiRecordV2 IosFullFfiChangeRecordV2(IosFullFfiRecordV2 value) {
  value.number += 2; value.fraction *= 3; value.extra += 5; return value;
}
int64_t IosFullFfiUpdateOnly(int64_t value) { return value + 400; }
bool IosFullFfiNegate(bool value) { return !value; }
void* IosFullFfiEchoPointer(void* value) { return value; }
int64_t IosFullFfiCallScalar(IosFullFfiScalarCallback callback, int64_t a, int64_t b) {
  return callback(a, b);
}
IosFullFfiRecordV2 IosFullFfiCallRecord(IosFullFfiRecordCallback callback,
                                     IosFullFfiRecordV2 value) {
  return callback(value);
}

static pthread_mutex_t worker_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_t workers[2];
static int indices[2] = {0, 1};
static int started;
static bool active;
static _Atomic int finished;
static int64_t results[2];
static IosFullFfiScalarCallback group_callback;
static IosFullFfiListener scalar_listener;
static IosFullFfiRecordListener compound_listener;

static void* RunWorker(void* argument) {
  const int index = *(const int*)argument;
  results[index] = group_callback(10 + index, 5);
  scalar_listener(index, results[index]);
  IosFullFfiRecordV2 payload = {21 + index, 2.5 + index, 31 + index};
  compound_listener(index, payload);
  volatile unsigned char* bytes = (volatile unsigned char*)&payload;
  for (size_t i = 0; i < sizeof(payload); ++i) bytes[i] = 0xCD;
  // No callback or Dart entry occurs after this release. The mutator may then
  // join this already-quiescent worker without waiting for a Dart callback.
  atomic_fetch_add(&finished, 1);
  return NULL;
}

int32_t IosFullFfiStartWorkers(IosFullFfiScalarCallback group,
                              IosFullFfiListener listener,
                              IosFullFfiRecordListener record_listener) {
  if (!group || !listener || !record_listener) return -1;
  pthread_mutex_lock(&worker_lock);
  if (active) { pthread_mutex_unlock(&worker_lock); return -1; }
  active = true; started = 0; atomic_store(&finished, 0);
  group_callback = group; scalar_listener = listener; compound_listener = record_listener;
  for (int i = 0; i < 2; ++i) {
    if (pthread_create(&workers[i], NULL, RunWorker, &indices[i]) != 0) break;
    ++started;
  }
  if (started == 0) active = false;
  const int result = started;
  pthread_mutex_unlock(&worker_lock);
  return result;  // NEVER join here while the caller holds the Dart mutator.
}

int32_t IosFullFfiWorkersDone(void) {
  pthread_mutex_lock(&worker_lock);
  const bool done = active && atomic_load(&finished) == started;
  pthread_mutex_unlock(&worker_lock);
  return done ? 1 : 0;
}

int64_t IosFullFfiJoinWorkers(void) {
  pthread_mutex_lock(&worker_lock);
  if (!active || atomic_load(&finished) != started) {
    pthread_mutex_unlock(&worker_lock); return INT64_MIN;
  }
  int64_t sum = 0;
  for (int i = 0; i < started; ++i) {
    if (pthread_join(workers[i], NULL) != 0) {
      pthread_mutex_unlock(&worker_lock); return INT64_MIN;
    }
    sum += results[i];
  }
  active = false; started = 0;
  group_callback = NULL; scalar_listener = NULL; compound_listener = NULL;
  pthread_mutex_unlock(&worker_lock);
  return sum;
}

// Swift calls this before engine creation. Volatile references keep every
// update-only export in the released executable even under dead stripping.
// Runner's export_dynamic linker setting exposes them to process FFI lookup.
static void* volatile export_anchor;
int32_t IosFullFfiRetain(void) {
  static void* const exports[] = {
      (void*)&IosFullFfiVersion, (void*)&IosFullFfiScalarV1,
      (void*)&IosFullFfiScalarV2, (void*)&IosFullFfiChangeRecordV1,
      (void*)&IosFullFfiChangeRecordV2, (void*)&IosFullFfiUpdateOnly,
      (void*)&IosFullFfiNegate, (void*)&IosFullFfiEchoPointer,
      (void*)&IosFullFfiCallScalar, (void*)&IosFullFfiCallRecord,
      (void*)&IosFullFfiStartWorkers, (void*)&IosFullFfiWorkersDone,
      (void*)&IosFullFfiJoinWorkers,
  };
  const int32_t count = sizeof(exports) / sizeof(exports[0]);
  for (int32_t i = 0; i < count; ++i) export_anchor = exports[i];
  return count;
}
