#ifndef LICENSE_PATCH_FULL_FFI_FIXTURE_H_
#define LICENSE_PATCH_FULL_FFI_FIXTURE_H_

#include <stdbool.h>
#include <stdint.h>

#define IOS_FULL_FFI_EXPORT __attribute__((visibility("default"), used))

typedef struct { int64_t number; double fraction; } IosFullFfiRecordV1;
typedef struct { int64_t number; double fraction; int64_t extra; } IosFullFfiRecordV2;
typedef int64_t (*IosFullFfiScalarCallback)(int64_t, int64_t);
typedef IosFullFfiRecordV2 (*IosFullFfiRecordCallback)(IosFullFfiRecordV2);
typedef void (*IosFullFfiListener)(int64_t, int64_t);
typedef void (*IosFullFfiRecordListener)(int64_t, IosFullFfiRecordV2);

IOS_FULL_FFI_EXPORT int32_t IosFullFfiRetain(void);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiVersion(void);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiScalarV1(int64_t value);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiScalarV2(int64_t value);
IOS_FULL_FFI_EXPORT IosFullFfiRecordV1 IosFullFfiChangeRecordV1(IosFullFfiRecordV1 value);
IOS_FULL_FFI_EXPORT IosFullFfiRecordV2 IosFullFfiChangeRecordV2(IosFullFfiRecordV2 value);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiUpdateOnly(int64_t value);
IOS_FULL_FFI_EXPORT bool IosFullFfiNegate(bool value);
IOS_FULL_FFI_EXPORT void* IosFullFfiEchoPointer(void* value);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiCallScalar(IosFullFfiScalarCallback callback, int64_t a, int64_t b);
IOS_FULL_FFI_EXPORT IosFullFfiRecordV2 IosFullFfiCallRecord(IosFullFfiRecordCallback callback, IosFullFfiRecordV2 value);
IOS_FULL_FFI_EXPORT int32_t IosFullFfiStartWorkers(IosFullFfiScalarCallback group, IosFullFfiListener listener, IosFullFfiRecordListener record_listener);
IOS_FULL_FFI_EXPORT int32_t IosFullFfiWorkersDone(void);
IOS_FULL_FFI_EXPORT int64_t IosFullFfiJoinWorkers(void);

#endif
