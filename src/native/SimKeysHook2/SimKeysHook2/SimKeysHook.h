#pragma once

#include <windows.h>

#if defined(SIMKEYSHOOK2_EXPORTS)
#define SIMKEYS_API extern "C" __declspec(dllexport)
#else
#define SIMKEYS_API extern "C" __declspec(dllimport)
#endif

// Exported entrypoint invoked by the injector after LoadLibrary completes.
// The LPVOID matches CreateRemoteThread's expected thread start signature.
SIMKEYS_API DWORD WINAPI InitSimKeys(LPVOID reserved);
