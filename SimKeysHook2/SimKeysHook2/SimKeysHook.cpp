#include "SimKeysHook.h"

#include <strsafe.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>

#if defined(_M_IX86)
#pragma comment(linker, "/EXPORT:InitSimKeys=_InitSimKeys@4")
#elif defined(_M_X64)
#pragma comment(linker, "/EXPORT:InitSimKeys=InitSimKeys")
#endif

namespace {

static_assert(sizeof(void*) == 4, "Build the SimKeys hook as Win32 for NWN Diamond.");

constexpr UINT kOpQuery = 3000;
constexpr UINT kOpTriggerSlot = 3001;
constexpr UINT kOpTriggerVk = 3002;
constexpr UINT kOpSetLog = 3003;
constexpr UINT kOpReplayLast = 3004;
constexpr UINT kOpSnapshotText = 3005;
constexpr UINT kOpChatSend = 3006;
constexpr UINT kOpChatPoll = 3007;
constexpr UINT kOpTriggerPageSlot = 3008;

constexpr UINT kMsgTriggerVk = WM_APP + 0x491;
constexpr UINT kMsgSendChat = WM_APP + 0x492;
constexpr UINT kMsgRefreshIdentity = WM_APP + 0x493;
constexpr UINT kMsgTriggerPageSlot = WM_APP + 0x494;
constexpr DWORD kPipeBufferSize = 8192;
constexpr DWORD kDispatchTimeoutMs = 2000;
constexpr DWORD kPipeStartupTimeoutMs = 2000;
constexpr uintptr_t kPreferredImageBase = 0x00400000u;
constexpr uintptr_t kAppGlobalSlotAddress = 0x0092DC50u;
constexpr UINT kExpectedNwnWndProc = 0x00403F90;
constexpr UINT kExpectedKeyPreDispatch = 0x00403B90;
constexpr UINT kExpectedGate90Accessor = 0x00407790;
constexpr UINT kExpectedGate94Accessor = 0x004077A0;
constexpr UINT kExpectedGate98Accessor = 0x004077B0;
constexpr UINT kExpectedDispatcherAccessor = 0x004077F0;
constexpr UINT kExpectedDispatcherThunk = 0x0076AF10;
constexpr UINT kExpectedDispatcherSlot0 = 0x005905E0;
constexpr UINT kExpectedQuickbarExec = 0x0051FAA0;
constexpr UINT kExpectedQuickbarPageSelect = 0x0051FD10;
constexpr UINT kExpectedQuickbarSlotDispatch = 0x005164A0;
constexpr UINT kExpectedQuickbarVtable = 0x008AB6D0;
constexpr UINT kExpectedObjectByIdResolver = 0x004078C0;
constexpr UINT kExpectedItemEquippedOwnerResolver = 0x004E9B50;
constexpr UINT kExpectedChatSend = 0x0057C9F0;
constexpr UINT kExpectedChatWindowLog = 0x00493BD0;
constexpr UINT kExpectedAppObjectResolver = 0x00405160;
constexpr UINT kExpectedCurrentPlayerResolver = 0x00407850;
constexpr UINT kExpectedPlayerNameBuilder = 0x004CEF20;
constexpr UINT kExpectedNwnStringDestroy = 0x005BA420;
constexpr uint32_t kQuickbarPanelSlotsOffset = 0x68u;
constexpr uint32_t kQuickbarCurrentPageOffset = 0x2BB8u;
constexpr uint32_t kQuickbarPageStride = 0xE70u;
constexpr uint32_t kQuickbarSlotStride = 0x134u;
constexpr uint32_t kQuickbarSlotPrimaryItemOffset = 0x50u;
constexpr uint32_t kQuickbarSlotSecondaryItemOffset = 0x54u;
constexpr uint32_t kQuickbarSlotTypeOffset = 0x84u;
constexpr uint32_t kInvalidObjectId = 0x7F000000u;
constexpr BYTE kQuickbarItemSlotType = 1;
constexpr int kQuickbarPageCount = 3;
constexpr int kQuickbarSlotCount = 12;
constexpr int kPendingChatCapacity = 1024;
constexpr int kChatQueueCapacity = 128;
constexpr int kChatTextCapacity = 768;
constexpr int kCharacterNameCapacity = 128;

enum LogLevel {
  kLogError = 0,
  kLogInfo = 1,
  kLogDebug = 2,
};

struct PipeHeader {
  uint32_t op;
  uint32_t size;
};

struct QueryResponse {
  uint32_t module_base;
  uint32_t hook_wndproc;
  uint32_t hwnd;
  uint32_t current_wndproc;
  uint32_t original_wndproc;
  uint32_t window_thread_id;
  uint32_t installed;
  uint32_t expected_runtime_nwn_wndproc;
  uint32_t expected_runtime_key_pre_dispatch;
  uint32_t expected_runtime_dispatcher_thunk;
  uint32_t expected_runtime_dispatcher_slot0;
  uint32_t app_global_slot;
  uint32_t app_holder;
  uint32_t app_object;
  uint32_t app_inner;
  uint32_t dispatcher_ptr;
  uint32_t gate_90;
  uint32_t gate_94;
  uint32_t gate_98;
  uint32_t quickbar_exec;
  uint32_t quickbar_slot_dispatch;
  uint32_t quickbar_panel_vtable;
  uint32_t quickbar_slot_ptr;
  uint32_t quickbar_this;
  int32_t quickbar_page;
  int32_t quickbar_slot;
  int32_t quickbar_slot_type;
  int32_t quickbar_calls;
  int32_t quickbar_scan_attempts;
  int32_t quickbar_scan_hits;
  int32_t last_vk;
  int32_t last_rc;
  int32_t last_error;
  int32_t log_level;
  uint32_t player_object;
  int32_t identity_refresh_count;
  int32_t identity_error;
  uint32_t quickbar_item_mask_low;
  uint32_t quickbar_item_mask_high;
  uint32_t quickbar_equipped_mask_low;
  uint32_t quickbar_equipped_mask_high;
  char character_name[kCharacterNameCapacity];
};

struct TriggerResponse {
  int32_t success;
  int32_t vk;
  int32_t rc;
  int32_t aux_rc;
  int32_t last_error;
  int32_t path;
};

struct ChatSendResponse {
  int32_t success;
  int32_t mode;
  int32_t rc;
  int32_t last_error;
};

struct ChatPollRequest {
  int32_t after_sequence;
  int32_t max_lines;
};

struct ChatPollResponseHeader {
  int32_t latest_sequence;
  int32_t line_count;
};

struct ChatPollLineHeader {
  int32_t sequence;
  int32_t text_length;
};

struct PendingChatDispatch {
  HANDLE event;
  volatile LONG busy;
  volatile LONG sequence_seed;
  volatile LONG request_id;
  volatile LONG mode;
  volatile LONG result;
  volatile LONG last_error;
  char text[kPendingChatCapacity];
};

struct ChatLineEntry {
  int32_t sequence;
  char text[kChatTextCapacity];
};

struct PendingDispatch {
  HANDLE event;
  volatile LONG busy;
  volatile LONG sequence_seed;
  volatile LONG request_id;
  volatile LONG vk;
  volatile LONG result;
  volatile LONG aux_result;
  volatile LONG dispatch_path;
  volatile LONG last_error;
};

struct PendingIdentityDispatch {
  HANDLE event;
  volatile LONG busy;
  volatile LONG sequence_seed;
  volatile LONG request_id;
  volatile LONG last_error;
};

struct SimKeysState {
  HMODULE module;
  HWND hwnd;
  WNDPROC original_wndproc;
  HANDLE pipe_thread;
  HANDLE pipe_ready_event;
  CRITICAL_SECTION lock;
  bool lock_ready;
  CRITICAL_SECTION chat_lock;
  bool chat_lock_ready;
  CRITICAL_SECTION log_lock;
  bool log_lock_ready;
  DWORD window_thread_id;
  PendingDispatch pending;
  PendingChatDispatch pending_chat;
  PendingIdentityDispatch pending_identity;
  HANDLE log_file;
  char module_path[MAX_PATH];
  char log_path[MAX_PATH];
  char character_name[kCharacterNameCapacity];
  volatile LONG initialized;
  volatile LONG installed;
  volatile LONG pipe_state;
  volatile LONG pipe_thread_error;
  volatile LONG quickbar_trace_installed;
  volatile LONG quickbar_slot_trace_installed;
  volatile LONG chat_trace_installed;
  volatile LONG chat_write_index;
  volatile LONG chat_count;
  volatile LONG chat_sequence;
  volatile LONG last_chat_mode;
  volatile LONG last_chat_result;
  volatile LONG last_chat_error;
  volatile LONG quickbar_this;
  volatile LONG quickbar_page;
  volatile LONG quickbar_slot;
  volatile LONG quickbar_slot_type;
  volatile LONG quickbar_slot_ptr;
  volatile LONG quickbar_calls;
  volatile LONG quickbar_scan_attempts;
  volatile LONG quickbar_scan_hits;
  volatile LONG quickbar_item_mask_low;
  volatile LONG quickbar_item_mask_high;
  volatile LONG quickbar_equipped_mask_low;
  volatile LONG quickbar_equipped_mask_high;
  volatile LONG log_level;
  volatile LONG player_object;
  volatile LONG identity_refresh_count;
  volatile LONG identity_error;
  volatile LONG last_vk;
  volatile LONG last_result;
  volatile LONG last_error;
  ChatLineEntry chat_lines[kChatQueueCapacity];
};

SimKeysState g_state = {};

BYTE g_quickbar_exec_original[16] = {};
void* g_quickbar_exec_gateway = nullptr;
size_t g_quickbar_exec_stolen = 0;
BYTE g_quickbar_slot_original[16] = {};
void* g_quickbar_slot_gateway = nullptr;
size_t g_quickbar_slot_stolen = 0;
BYTE g_chat_log_original[32] = {};
void* g_chat_log_gateway = nullptr;
size_t g_chat_log_stolen = 0;

LRESULT CALLBACK SimKeysWndProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam);
void LogMessage(int level, const char* format, ...);
void WriteExecutableMemory(void* destination, const void* source, SIZE_T size);
void* MakeJmpGateway(BYTE* target, size_t stolen);
BOOL DiscoverQuickbarPanelByScan(const char* reason);
BOOL InstallChatWindowLogHook();
BOOL RefreshCharacterIdentity(DWORD* out_error);
void UpdateQuickbarItemMasksOnWindowThread();

uintptr_t GetProcessImageBase() {
  return reinterpret_cast<uintptr_t>(GetModuleHandleA(nullptr));
}

uint32_t RebaseAddress(uint32_t preferred_absolute) {
  const uintptr_t image_base = GetProcessImageBase();
  if (image_base == 0 || preferred_absolute < kPreferredImageBase) {
    return preferred_absolute;
  }
  return static_cast<uint32_t>(image_base + (preferred_absolute - kPreferredImageBase));
}

template <typename T>
bool SafeReadValue(uintptr_t address, T* out) {
  if (out == nullptr) {
    return false;
  }
  __try {
    *out = *reinterpret_cast<const T*>(address);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    ZeroMemory(out, sizeof(T));
    return false;
  }
}

uint32_t SafeReadPointer32(uintptr_t address) {
  uint32_t value = 0;
  SafeReadValue(address, &value);
  return value;
}

LONG QuickbarSlotTypeToCaseIndex(LONG raw_slot_type) {
  return raw_slot_type > 0 ? (raw_slot_type - 1) : -1;
}

uint32_t ReadAppHolderPointer() {
  return SafeReadPointer32(kAppGlobalSlotAddress);
}

uint32_t ReadAppObjectPointer() {
  const uint32_t holder = ReadAppHolderPointer();
  return holder != 0 ? SafeReadPointer32(holder) : 0;
}

uint32_t ReadAppInnerPointer() {
  const uint32_t app_object = ReadAppObjectPointer();
  return app_object != 0 ? SafeReadPointer32(static_cast<uintptr_t>(app_object) + 4) : 0;
}

uint32_t ReadCurrentPlayerObjectId() {
  const uint32_t app_inner = ReadAppInnerPointer();
  return app_inner != 0 ? SafeReadPointer32(static_cast<uintptr_t>(app_inner) + 0x20u) : 0;
}

bool IsValidObjectId(uint32_t object_id) {
  return object_id != 0 && object_id != kInvalidObjectId;
}

void SetQuickbarMaskBit(uint32_t* low, uint32_t* high, int bit_index) {
  if (low == nullptr || high == nullptr || bit_index < 0) {
    return;
  }

  if (bit_index < 32) {
    *low |= (1u << bit_index);
  } else if (bit_index < kQuickbarPageCount * kQuickbarSlotCount) {
    *high |= (1u << (bit_index - 32));
  }
}

void StoreQuickbarItemMasks(uint32_t item_low, uint32_t item_high, uint32_t equipped_low, uint32_t equipped_high) {
  InterlockedExchange(&g_state.quickbar_item_mask_low, static_cast<LONG>(item_low));
  InterlockedExchange(&g_state.quickbar_item_mask_high, static_cast<LONG>(item_high));
  InterlockedExchange(&g_state.quickbar_equipped_mask_low, static_cast<LONG>(equipped_low));
  InterlockedExchange(&g_state.quickbar_equipped_mask_high, static_cast<LONG>(equipped_high));
}

void UpdateQuickbarItemMasksOnWindowThread() {
  typedef void* (__thiscall* ResolveObjectByIdFn)(void* app_object, uint32_t object_id);
  typedef void* (__thiscall* ItemEquippedOwnerFn)(void* item_object);

  uint32_t item_low = 0;
  uint32_t item_high = 0;
  uint32_t equipped_low = 0;
  uint32_t equipped_high = 0;

  __try {
    const uint32_t panel = static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_this, 0, 0));
    if (panel == 0 || SafeReadPointer32(panel) != RebaseAddress(kExpectedQuickbarVtable)) {
      StoreQuickbarItemMasks(0, 0, 0, 0);
      return;
    }

    const uint32_t app_object = ReadAppObjectPointer();
    const uint32_t current_player_object_id = ReadCurrentPlayerObjectId();
    if (app_object == 0 || current_player_object_id == 0) {
      StoreQuickbarItemMasks(0, 0, 0, 0);
      return;
    }

    const ResolveObjectByIdFn resolve_object =
        reinterpret_cast<ResolveObjectByIdFn>(RebaseAddress(kExpectedObjectByIdResolver));
    const ItemEquippedOwnerFn item_equipped_owner =
        reinterpret_cast<ItemEquippedOwnerFn>(RebaseAddress(kExpectedItemEquippedOwnerResolver));

    for (int page = 0; page < kQuickbarPageCount; ++page) {
      for (int slot = 0; slot < kQuickbarSlotCount; ++slot) {
        const int bit_index = page * kQuickbarSlotCount + slot;
        const uint32_t slot_ptr = panel +
            kQuickbarPanelSlotsOffset +
            static_cast<uint32_t>(page) * kQuickbarPageStride +
            static_cast<uint32_t>(slot) * kQuickbarSlotStride;

        BYTE slot_type = 0;
        if (!SafeReadValue(static_cast<uintptr_t>(slot_ptr) + kQuickbarSlotTypeOffset, &slot_type) ||
            slot_type != kQuickbarItemSlotType) {
          continue;
        }

        const uint32_t primary_item_id = SafeReadPointer32(static_cast<uintptr_t>(slot_ptr) + kQuickbarSlotPrimaryItemOffset);
        if (!IsValidObjectId(primary_item_id)) {
          continue;
        }

        void* primary_item = resolve_object(reinterpret_cast<void*>(app_object), primary_item_id);
        if (primary_item == nullptr) {
          continue;
        }

        SetQuickbarMaskBit(&item_low, &item_high, bit_index);

        void* primary_owner = item_equipped_owner(primary_item);
        if (primary_owner == nullptr ||
            SafeReadPointer32(reinterpret_cast<uintptr_t>(primary_owner) + 4u) != current_player_object_id) {
          continue;
        }

        const uint32_t secondary_item_id = SafeReadPointer32(static_cast<uintptr_t>(slot_ptr) + kQuickbarSlotSecondaryItemOffset);
        if (IsValidObjectId(secondary_item_id)) {
          void* secondary_item = resolve_object(reinterpret_cast<void*>(app_object), secondary_item_id);
          if (secondary_item == nullptr) {
            continue;
          }
          void* secondary_owner = item_equipped_owner(secondary_item);
          if (secondary_owner == nullptr ||
              SafeReadPointer32(reinterpret_cast<uintptr_t>(secondary_owner) + 4u) != current_player_object_id) {
            continue;
          }
        }

        SetQuickbarMaskBit(&equipped_low, &equipped_high, bit_index);
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    StoreQuickbarItemMasks(0, 0, 0, 0);
    LogMessage(
        kLogError,
        "quickbar item mask refresh raised exception code=0x%08lX",
        static_cast<unsigned long>(GetExceptionCode()));
    return;
  }

  StoreQuickbarItemMasks(item_low, item_high, equipped_low, equipped_high);
  LogMessage(
      kLogDebug,
      "quickbar item masks refreshed item=0x%08X%08X equipped=0x%08X%08X",
      item_high,
      item_low,
      equipped_high,
      equipped_low);
}

void CopyStoredCharacterName(char* out, size_t capacity) {
  if (out == nullptr || capacity == 0) {
    return;
  }

  out[0] = '\0';
  if (g_state.lock_ready) {
    EnterCriticalSection(&g_state.lock);
    StringCchCopyA(out, capacity, g_state.character_name);
    LeaveCriticalSection(&g_state.lock);
    return;
  }

  StringCchCopyA(out, capacity, g_state.character_name);
}

void StoreCharacterName(const char* text) {
  const char* resolved = text != nullptr ? text : "";
  if (g_state.lock_ready) {
    EnterCriticalSection(&g_state.lock);
    strncpy_s(g_state.character_name, sizeof(g_state.character_name), resolved, _TRUNCATE);
    LeaveCriticalSection(&g_state.lock);
    return;
  }

  strncpy_s(g_state.character_name, sizeof(g_state.character_name), resolved, _TRUNCATE);
}

bool IsReadableWritableProtection(DWORD protect) {
  const DWORD basic = protect & 0xFFu;
  return basic == PAGE_READWRITE ||
      basic == PAGE_WRITECOPY ||
      basic == PAGE_EXECUTE_READWRITE ||
      basic == PAGE_EXECUTE_WRITECOPY;
}

BOOL TryAdoptQuickbarPanel(uint32_t panel_ptr, LONG slot_index, LONG page_index, const char* source) {
  if (panel_ptr == 0) {
    return FALSE;
  }

  const uint32_t expected_vtable = RebaseAddress(kExpectedQuickbarVtable);
  const uint32_t expected_slot_dispatch = RebaseAddress(kExpectedQuickbarSlotDispatch);
  if (SafeReadPointer32(panel_ptr) != expected_vtable) {
    return FALSE;
  }

  const uint32_t current_page_base = SafeReadPointer32(static_cast<uintptr_t>(panel_ptr) + kQuickbarCurrentPageOffset);
  LONG resolved_page = page_index;
  bool page_matches = false;
  for (LONG page = 0; page < kQuickbarPageCount; ++page) {
    const uint32_t expected_page_base = panel_ptr + kQuickbarPanelSlotsOffset + static_cast<uint32_t>(page) * kQuickbarPageStride;
    if (current_page_base == expected_page_base) {
      if (resolved_page < 0) {
        resolved_page = page;
      }
      page_matches = page == resolved_page;
      if (page_matches) {
        break;
      }
    }
  }
  if (!page_matches) {
    return FALSE;
  }

  if (SafeReadPointer32(static_cast<uintptr_t>(current_page_base) + 0x2Cu) != expected_slot_dispatch) {
    return FALSE;
  }

  const LONG previous_this = InterlockedExchange(&g_state.quickbar_this, static_cast<LONG>(panel_ptr));
  InterlockedExchange(&g_state.quickbar_page, resolved_page);
  if (slot_index >= 0) {
    InterlockedExchange(&g_state.quickbar_slot, slot_index);
  }
  if (previous_this != static_cast<LONG>(panel_ptr)) {
    LogMessage(
        kLogInfo,
        "quickbar panel captured via %s panel=0x%08X page=%ld slot=%ld currentPageBase=0x%08X",
        source != nullptr ? source : "unknown",
        panel_ptr,
        resolved_page,
        slot_index,
        current_page_base);
  }
  return TRUE;
}

BOOL TryDeriveQuickbarPanelFromSlot(uint32_t slot_ptr, LONG* out_panel, LONG* out_slot_index, LONG* out_page_index) {
  if (slot_ptr == 0) {
    return FALSE;
  }

  for (LONG page = 0; page < kQuickbarPageCount; ++page) {
    for (LONG slot = 0; slot < kQuickbarSlotCount; ++slot) {
      const uint32_t delta = kQuickbarPanelSlotsOffset +
          static_cast<uint32_t>(page) * kQuickbarPageStride +
          static_cast<uint32_t>(slot) * kQuickbarSlotStride;
      if (slot_ptr < delta) {
        continue;
      }

      const uint32_t panel_ptr = slot_ptr - delta;
      if (!TryAdoptQuickbarPanel(panel_ptr, slot, page, "slot-trace")) {
        continue;
      }

      if (out_panel != nullptr) {
        *out_panel = static_cast<LONG>(panel_ptr);
      }
      if (out_slot_index != nullptr) {
        *out_slot_index = slot;
      }
      if (out_page_index != nullptr) {
        *out_page_index = page;
      }
      return TRUE;
    }
  }

  return FALSE;
}

BOOL DiscoverQuickbarPanelByScan(const char* reason) {
  const LONG attempt = InterlockedIncrement(&g_state.quickbar_scan_attempts);
  const uint32_t expected_vtable = RebaseAddress(kExpectedQuickbarVtable);
  const uint32_t expected_slot_dispatch = RebaseAddress(kExpectedQuickbarSlotDispatch);
  SYSTEM_INFO system_info = {};
  GetSystemInfo(&system_info);

  LONG matches = 0;
  uint32_t found_panel = 0;
  LONG found_page = -1;

  LogMessage(
      kLogDebug,
      "quickbar scan starting attempt=%ld reason=%s min=0x%08X max=0x%08X",
      attempt,
      reason != nullptr ? reason : "scan",
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(system_info.lpMinimumApplicationAddress)),
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(system_info.lpMaximumApplicationAddress)));

  __try {
    uintptr_t cursor = reinterpret_cast<uintptr_t>(system_info.lpMinimumApplicationAddress);
    const uintptr_t maximum = reinterpret_cast<uintptr_t>(system_info.lpMaximumApplicationAddress);
    while (cursor < maximum) {
      MEMORY_BASIC_INFORMATION mbi = {};
      if (VirtualQuery(reinterpret_cast<LPCVOID>(cursor), &mbi, sizeof(mbi)) != sizeof(mbi)) {
        break;
      }

      const uintptr_t region_base = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
      const uintptr_t region_end = region_base + mbi.RegionSize;
      if (region_end < region_base) {
        break;
      }

      if (mbi.State == MEM_COMMIT &&
          (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS)) == 0 &&
          IsReadableWritableProtection(mbi.Protect) &&
          region_end > region_base + kQuickbarCurrentPageOffset + sizeof(uint32_t)) {
        const uintptr_t limit = region_end - (kQuickbarCurrentPageOffset + sizeof(uint32_t));
        for (uintptr_t candidate = region_base; candidate <= limit; candidate += sizeof(uint32_t)) {
          const uint32_t candidate_vtable = SafeReadPointer32(candidate);
          if (candidate_vtable != expected_vtable) {
            continue;
          }

          const uint32_t current_page_base = SafeReadPointer32(candidate + kQuickbarCurrentPageOffset);
          if (current_page_base == 0) {
            continue;
          }
          if (SafeReadPointer32(static_cast<uintptr_t>(current_page_base) + 0x2Cu) != expected_slot_dispatch) {
            continue;
          }

          LONG page_match = -1;
          for (LONG page = 0; page < kQuickbarPageCount; ++page) {
            const uint32_t expected_page_base =
                static_cast<uint32_t>(candidate) + kQuickbarPanelSlotsOffset + static_cast<uint32_t>(page) * kQuickbarPageStride;
            if (current_page_base == expected_page_base) {
              page_match = page;
              break;
            }
          }
          if (page_match < 0) {
            continue;
          }

          found_panel = static_cast<uint32_t>(candidate);
          found_page = page_match;
          ++matches;
          if (matches > 1) {
            break;
          }
        }
      }

      if (matches > 1) {
        break;
      }
      cursor = region_end;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    LogMessage(
        kLogError,
        "quickbar scan raised SEH exception attempt=%ld reason=%s",
        attempt,
        reason != nullptr ? reason : "scan");
    return FALSE;
  }

  if (matches == 1 && TryAdoptQuickbarPanel(found_panel, -1, found_page, "memory-scan")) {
    const LONG hits = InterlockedIncrement(&g_state.quickbar_scan_hits);
    LogMessage(
        kLogInfo,
        "quickbar scan found panel=0x%08X page=%ld attempt=%ld hits=%ld reason=%s",
        found_panel,
        found_page,
        attempt,
        hits,
        reason != nullptr ? reason : "scan");
    return TRUE;
  }

  LogMessage(
      kLogDebug,
      "quickbar scan found no unique panel attempt=%ld matches=%ld reason=%s",
      attempt,
      matches,
      reason != nullptr ? reason : "scan");
  return FALSE;
}

void __stdcall CaptureQuickbarExec(LONG quickbar_this, LONG slot_index) {
  TryAdoptQuickbarPanel(static_cast<uint32_t>(quickbar_this), slot_index, -1, "quickbar-exec");
  const LONG count = InterlockedIncrement(&g_state.quickbar_calls);
  if (count <= 5) {
    LogMessage(kLogDebug, "quickbar exec trace this=0x%08X slot=%ld calls=%ld", quickbar_this, slot_index, count);
  }
}

void __stdcall CaptureQuickbarSlotDispatch(LONG slot_ptr) {
  InterlockedExchange(&g_state.quickbar_slot_ptr, slot_ptr);
  BYTE slot_type = 0;
  SafeReadValue(static_cast<uintptr_t>(slot_ptr) + 0x84u, &slot_type);
  const LONG raw_slot_type = static_cast<LONG>(slot_type);
  const LONG slot_case = QuickbarSlotTypeToCaseIndex(raw_slot_type);
  InterlockedExchange(&g_state.quickbar_slot_type, raw_slot_type);

  LONG panel = 0;
  LONG slot_index = -1;
  LONG page_index = -1;
  if (TryDeriveQuickbarPanelFromSlot(static_cast<uint32_t>(slot_ptr), &panel, &slot_index, &page_index)) {
    LogMessage(
        kLogDebug,
        "quickbar slot trace slotPtr=0x%08X rawType=%u case=%ld panel=0x%08X page=%ld slot=%ld",
        slot_ptr,
        static_cast<unsigned int>(slot_type),
        slot_case,
        static_cast<uint32_t>(panel),
        page_index,
        slot_index);
  } else {
    LogMessage(
        kLogDebug,
        "quickbar slot trace slotPtr=0x%08X rawType=%u case=%ld (panel unresolved)",
        slot_ptr,
        static_cast<unsigned int>(slot_type),
        slot_case);
  }
}

bool ExtractNwnStringText(const void* nwn_string_object, char* out, size_t capacity) {
  if (out == nullptr || capacity == 0) {
    return false;
  }
  out[0] = '\0';

  if (nwn_string_object == nullptr) {
    return false;
  }

  uint32_t text_ptr = 0;
  int32_t text_length = 0;
  __try {
    text_ptr = *reinterpret_cast<const uint32_t*>(nwn_string_object);
    text_length = *reinterpret_cast<const int32_t*>(static_cast<const BYTE*>(nwn_string_object) + sizeof(uint32_t));
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }

  if (text_ptr == 0 || text_length <= 0) {
    return false;
  }

  size_t copy_length = static_cast<size_t>(text_length);
  if (copy_length >= capacity) {
    copy_length = capacity - 1;
  }

  __try {
    memcpy(out, reinterpret_cast<const void*>(text_ptr), copy_length);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    out[0] = '\0';
    return false;
  }

  out[copy_length] = '\0';
  return true;
}

void QueueChatLine(const char* text) {
  if (text == nullptr || text[0] == '\0' || !g_state.chat_lock_ready) {
    return;
  }

  EnterCriticalSection(&g_state.chat_lock);

  const LONG sequence = InterlockedIncrement(&g_state.chat_sequence);
  const LONG write_index = InterlockedCompareExchange(&g_state.chat_write_index, 0, 0);
  ChatLineEntry* entry = &g_state.chat_lines[write_index % kChatQueueCapacity];
  entry->sequence = sequence;
  strncpy_s(entry->text, sizeof(entry->text), text, _TRUNCATE);

  const LONG next_index = (write_index + 1) % kChatQueueCapacity;
  InterlockedExchange(&g_state.chat_write_index, next_index);
  const LONG existing_count = InterlockedCompareExchange(&g_state.chat_count, 0, 0);
  if (existing_count < kChatQueueCapacity) {
    InterlockedExchange(&g_state.chat_count, existing_count + 1);
  }

  LeaveCriticalSection(&g_state.chat_lock);

  LogMessage(kLogDebug, "chat line captured seq=%ld text=%s", sequence, text);
}

void __stdcall CaptureChatWindowLog(LONG chat_this, const void* nwn_string_object) {
  char text[kChatTextCapacity] = {};
  if (!ExtractNwnStringText(nwn_string_object, text, sizeof(text))) {
    LogMessage(kLogDebug, "chat log hook saw an unreadable string this=0x%08X", static_cast<uint32_t>(chat_this));
    return;
  }

  QueueChatLine(text);
}

BOOL BuildChatPollResponse(const ChatPollRequest& request, BYTE* out_payload, DWORD capacity, DWORD* out_size) {
  if (out_payload == nullptr || out_size == nullptr || capacity < sizeof(ChatPollResponseHeader) || !g_state.chat_lock_ready) {
    return FALSE;
  }

  ChatPollResponseHeader* response = reinterpret_cast<ChatPollResponseHeader*>(out_payload);
  response->latest_sequence = InterlockedCompareExchange(&g_state.chat_sequence, 0, 0);
  response->line_count = 0;
  DWORD offset = sizeof(ChatPollResponseHeader);

  EnterCriticalSection(&g_state.chat_lock);

  const LONG latest_sequence = InterlockedCompareExchange(&g_state.chat_sequence, 0, 0);
  const LONG chat_count = InterlockedCompareExchange(&g_state.chat_count, 0, 0);
  const LONG write_index = InterlockedCompareExchange(&g_state.chat_write_index, 0, 0);
  response->latest_sequence = latest_sequence;

  if (chat_count > 0 && latest_sequence > request.after_sequence) {
    LONG oldest_sequence = latest_sequence - chat_count + 1;
    if (oldest_sequence < 1) {
      oldest_sequence = 1;
    }

    LONG first_sequence = request.after_sequence + 1;
    if (first_sequence < oldest_sequence) {
      first_sequence = oldest_sequence;
    }

    LONG max_lines = request.max_lines;
    if (max_lines <= 0 || max_lines > kChatQueueCapacity) {
      max_lines = kChatQueueCapacity;
    }

    if (latest_sequence >= first_sequence && latest_sequence - first_sequence + 1 > max_lines) {
      first_sequence = latest_sequence - max_lines + 1;
    }

    const LONG oldest_index = (write_index - chat_count + kChatQueueCapacity) % kChatQueueCapacity;
    for (LONG sequence = first_sequence; sequence <= latest_sequence; ++sequence) {
      const LONG index = (oldest_index + (sequence - oldest_sequence)) % kChatQueueCapacity;
      const ChatLineEntry& entry = g_state.chat_lines[index];
      if (entry.sequence != sequence) {
        continue;
      }

      const size_t text_length = strnlen(entry.text, sizeof(entry.text));
      const DWORD needed = static_cast<DWORD>(sizeof(ChatPollLineHeader) + text_length);
      if (offset + needed > capacity) {
        break;
      }

      ChatPollLineHeader* line = reinterpret_cast<ChatPollLineHeader*>(out_payload + offset);
      line->sequence = entry.sequence;
      line->text_length = static_cast<int32_t>(text_length);
      offset += sizeof(ChatPollLineHeader);
      if (text_length > 0) {
        memcpy(out_payload + offset, entry.text, text_length);
        offset += static_cast<DWORD>(text_length);
      }
      ++response->line_count;
    }
  }

  LeaveCriticalSection(&g_state.chat_lock);

  *out_size = offset;
  return TRUE;
}

#if defined(_M_IX86)
extern "C" __declspec(naked) void QuickbarExecTraceThunk() {
  __asm {
    pushad
    mov  eax, dword ptr [esp + 32 + 4]
    push eax
    mov  eax, ecx
    push eax
    call CaptureQuickbarExec
    popad
    mov  eax, dword ptr [g_quickbar_exec_gateway]
    jmp  eax
  }
}

extern "C" __declspec(naked) void QuickbarSlotDispatchTraceThunk() {
  __asm {
    pushad
    mov  eax, ecx
    push eax
    call CaptureQuickbarSlotDispatch
    popad
    mov  eax, dword ptr [g_quickbar_slot_gateway]
    jmp  eax
  }
}

extern "C" __declspec(naked) void ChatWindowLogTraceThunk() {
  __asm {
    pushad
    lea  eax, [esp + 32 + 4]
    push eax
    mov  eax, ecx
    push eax
    call CaptureChatWindowLog
    popad
    mov  eax, dword ptr [g_chat_log_gateway]
    jmp  eax
  }
}
#endif

BOOL InstallQuickbarTraceHook() {
  if (InterlockedCompareExchange(&g_state.quickbar_trace_installed, 0, 0) != 0) {
    return TRUE;
  }

  BYTE* target = reinterpret_cast<BYTE*>(RebaseAddress(kExpectedQuickbarExec));
  const size_t stolen = 10;
  memcpy(g_quickbar_exec_original, target, stolen);
  g_quickbar_exec_gateway = MakeJmpGateway(target, stolen);
  if (g_quickbar_exec_gateway == nullptr) {
    SetLastError(ERROR_OUTOFMEMORY);
    return FALSE;
  }

  BYTE patch[10] = {};
  patch[0] = 0xE9;
  *reinterpret_cast<int32_t*>(&patch[1]) = static_cast<int32_t>(
      reinterpret_cast<BYTE*>(&QuickbarExecTraceThunk) - (target + 5));
  for (size_t i = 5; i < stolen; ++i) {
    patch[i] = 0x90;
  }
  WriteExecutableMemory(target, patch, stolen);
  g_quickbar_exec_stolen = stolen;
  InterlockedExchange(&g_state.quickbar_trace_installed, 1);
  LogMessage(
      kLogInfo,
      "installed quickbar exec trace hook at 0x%08X stolen=%u gateway=0x%08X",
      RebaseAddress(kExpectedQuickbarExec),
      static_cast<unsigned int>(stolen),
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(g_quickbar_exec_gateway)));
  return TRUE;
}

BOOL InstallQuickbarSlotTraceHook() {
  if (InterlockedCompareExchange(&g_state.quickbar_slot_trace_installed, 0, 0) != 0) {
    return TRUE;
  }

  BYTE* target = reinterpret_cast<BYTE*>(RebaseAddress(kExpectedQuickbarSlotDispatch));
  const size_t stolen = 6;
  memcpy(g_quickbar_slot_original, target, stolen);
  g_quickbar_slot_gateway = MakeJmpGateway(target, stolen);
  if (g_quickbar_slot_gateway == nullptr) {
    SetLastError(ERROR_OUTOFMEMORY);
    return FALSE;
  }

  BYTE patch[6] = {};
  patch[0] = 0xE9;
  *reinterpret_cast<int32_t*>(&patch[1]) = static_cast<int32_t>(
      reinterpret_cast<BYTE*>(&QuickbarSlotDispatchTraceThunk) - (target + 5));
  patch[5] = 0x90;
  WriteExecutableMemory(target, patch, stolen);
  g_quickbar_slot_stolen = stolen;
  InterlockedExchange(&g_state.quickbar_slot_trace_installed, 1);
  LogMessage(
      kLogInfo,
      "installed quickbar slot trace hook at 0x%08X stolen=%u gateway=0x%08X",
      RebaseAddress(kExpectedQuickbarSlotDispatch),
      static_cast<unsigned int>(stolen),
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(g_quickbar_slot_gateway)));
  return TRUE;
}

BOOL InstallChatWindowLogHook() {
  if (InterlockedCompareExchange(&g_state.chat_trace_installed, 0, 0) != 0) {
    return TRUE;
  }

  BYTE* target = reinterpret_cast<BYTE*>(RebaseAddress(kExpectedChatWindowLog));
  const size_t stolen = 21;
  memcpy(g_chat_log_original, target, stolen);
  g_chat_log_gateway = MakeJmpGateway(target, stolen);
  if (g_chat_log_gateway == nullptr) {
    SetLastError(ERROR_OUTOFMEMORY);
    return FALSE;
  }

  BYTE patch[21] = {};
  patch[0] = 0xE9;
  *reinterpret_cast<int32_t*>(&patch[1]) = static_cast<int32_t>(
      reinterpret_cast<BYTE*>(&ChatWindowLogTraceThunk) - (target + 5));
  for (size_t i = 5; i < stolen; ++i) {
    patch[i] = 0x90;
  }
  WriteExecutableMemory(target, patch, stolen);
  g_chat_log_stolen = stolen;
  InterlockedExchange(&g_state.chat_trace_installed, 1);
  LogMessage(
      kLogInfo,
      "installed chat window log hook at 0x%08X stolen=%u gateway=0x%08X",
      RebaseAddress(kExpectedChatWindowLog),
      static_cast<unsigned int>(stolen),
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(g_chat_log_gateway)));
  return TRUE;
}

LRESULT CallQuickbarExecDirect(int slot_index) {
  typedef void (__thiscall* QuickbarExecFn)(void* self, int slot_index);
  LONG quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
  if (quickbar_this == 0) {
    DiscoverQuickbarPanelByScan("direct-call");
    quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
  }
  if (quickbar_this == 0) {
    SetLastError(ERROR_NOT_FOUND);
    return 0;
  }

  QuickbarExecFn fn = reinterpret_cast<QuickbarExecFn>(RebaseAddress(kExpectedQuickbarExec));
  fn(reinterpret_cast<void*>(quickbar_this), slot_index);
  InterlockedExchange(&g_state.quickbar_slot, slot_index);
  return 1;
}

LONG ResolveQuickbarPageIndex(uint32_t panel_ptr) {
  if (panel_ptr == 0) {
    return -1;
  }

  const uint32_t current_page_base = SafeReadPointer32(static_cast<uintptr_t>(panel_ptr) + kQuickbarCurrentPageOffset);
  if (current_page_base == 0) {
    return -1;
  }

  for (LONG page = 0; page < kQuickbarPageCount; ++page) {
    const uint32_t expected_page_base =
        panel_ptr + kQuickbarPanelSlotsOffset + static_cast<uint32_t>(page) * kQuickbarPageStride;
    if (current_page_base == expected_page_base) {
      return page;
    }
  }

  return -1;
}

BOOL CallQuickbarPageSelectDirect(int page_index, LONG* out_resolved_page) {
  typedef void (__thiscall* QuickbarPageSelectFn)(void* self, int page_index);

  if (page_index < 0 || page_index >= kQuickbarPageCount) {
    SetLastError(ERROR_INVALID_PARAMETER);
    return FALSE;
  }

  LONG quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
  if (quickbar_this == 0) {
    DiscoverQuickbarPanelByScan("direct-page-select");
    quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
  }
  if (quickbar_this == 0) {
    SetLastError(ERROR_NOT_FOUND);
    return FALSE;
  }

  const LONG previous_page = ResolveQuickbarPageIndex(static_cast<uint32_t>(quickbar_this));
  QuickbarPageSelectFn fn = reinterpret_cast<QuickbarPageSelectFn>(RebaseAddress(kExpectedQuickbarPageSelect));
  fn(reinterpret_cast<void*>(quickbar_this), page_index);

  const LONG resolved_page = ResolveQuickbarPageIndex(static_cast<uint32_t>(quickbar_this));
  if (out_resolved_page != nullptr) {
    *out_resolved_page = resolved_page;
  }

  if (resolved_page >= 0) {
    TryAdoptQuickbarPanel(static_cast<uint32_t>(quickbar_this), -1, resolved_page, "direct-page-select");
  }

  LogMessage(
      kLogDebug,
      "quickbar page select direct panel=0x%08X request=%d previous=%ld resolved=%ld",
      static_cast<unsigned int>(quickbar_this),
      page_index,
      previous_page,
      resolved_page);

  if (resolved_page != page_index) {
    SetLastError(ERROR_INVALID_STATE);
    return FALSE;
  }

  return TRUE;
}

void AppendFormat(char* buffer, size_t capacity, size_t* offset, const char* format, ...) {
  if (buffer == nullptr || offset == nullptr || *offset >= capacity) {
    return;
  }

  va_list args;
  va_start(args, format);
  const int written = _vsnprintf_s(buffer + *offset, capacity - *offset, _TRUNCATE, format, args);
  va_end(args);

  if (written < 0) {
    *offset = strlen(buffer);
  } else {
    *offset += static_cast<size_t>(written);
  }
}

void WriteExecutableMemory(void* destination, const void* source, SIZE_T size) {
  DWORD old_protect = 0;
  if (!VirtualProtect(destination, size, PAGE_EXECUTE_READWRITE, &old_protect)) {
    return;
  }
  memcpy(destination, source, size);
  DWORD ignored = 0;
  VirtualProtect(destination, size, old_protect, &ignored);
  FlushInstructionCache(GetCurrentProcess(), destination, size);
}

void* MakeJmpGateway(BYTE* target, size_t stolen) {
  BYTE* gateway = static_cast<BYTE*>(VirtualAlloc(nullptr, stolen + 5, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
  if (gateway == nullptr) {
    return nullptr;
  }

  memcpy(gateway, target, stolen);
  const intptr_t return_rel = reinterpret_cast<intptr_t>(target + stolen) - reinterpret_cast<intptr_t>(gateway + stolen + 5);
  gateway[stolen] = 0xE9;
  *reinterpret_cast<int32_t*>(gateway + stolen + 1) = static_cast<int32_t>(return_rel);
  return gateway;
}

bool BuildLogDirectory(const char* module_path, char* log_dir, size_t capacity) {
  if (module_path == nullptr || log_dir == nullptr || capacity == 0) {
    return false;
  }

  HRESULT hr = StringCchCopyA(log_dir, capacity, module_path);
  if (FAILED(hr)) {
    return false;
  }

  char* last_slash = strrchr(log_dir, '\\');
  if (last_slash == nullptr) {
    return false;
  }
  *last_slash = '\0';

  char* leaf = strrchr(log_dir, '\\');
  if (leaf != nullptr) {
    const char* leaf_name = leaf + 1;
    if (_stricmp(leaf_name, "Release") == 0 || _stricmp(leaf_name, "Debug") == 0) {
      *leaf = '\0';
    }
  }

  hr = StringCchCatA(log_dir, capacity, "\\logs");
  return SUCCEEDED(hr);
}

void EnsureLogFileReady() {
  if (!g_state.log_lock_ready || g_state.log_file != nullptr) {
    return;
  }

  char module_path[MAX_PATH] = {};
  if (GetModuleFileNameA(g_state.module, module_path, ARRAYSIZE(module_path)) == 0) {
    return;
  }

  StringCchCopyA(g_state.module_path, ARRAYSIZE(g_state.module_path), module_path);

  char log_dir[MAX_PATH] = {};
  if (!BuildLogDirectory(module_path, log_dir, ARRAYSIZE(log_dir))) {
    return;
  }
  CreateDirectoryA(log_dir, nullptr);

  StringCchPrintfA(g_state.log_path, ARRAYSIZE(g_state.log_path), "%s\\simkeys_%lu.log", log_dir, GetCurrentProcessId());
  g_state.log_file = CreateFileA(
      g_state.log_path,
      FILE_APPEND_DATA,
      FILE_SHARE_READ | FILE_SHARE_WRITE,
      nullptr,
      OPEN_ALWAYS,
      FILE_ATTRIBUTE_NORMAL,
      nullptr);

  if (g_state.log_file == INVALID_HANDLE_VALUE) {
    g_state.log_file = nullptr;
    g_state.log_path[0] = '\0';
  }
}

void LogMessage(int level, const char* format, ...) {
  if (level > InterlockedCompareExchange(&g_state.log_level, 0, 0)) {
    return;
  }

  char buffer[512] = {};
  va_list args;
  va_start(args, format);
  vsnprintf_s(buffer, sizeof(buffer), _TRUNCATE, format, args);
  va_end(args);

  SYSTEMTIME now = {};
  GetLocalTime(&now);

  char final_buffer[896] = {};
  _snprintf_s(
      final_buffer,
      sizeof(final_buffer),
      _TRUNCATE,
      "[simkeys][%04u-%02u-%02u %02u:%02u:%02u.%03u][pid=%lu][tid=%lu][L%d] %s\r\n",
      now.wYear,
      now.wMonth,
      now.wDay,
      now.wHour,
      now.wMinute,
      now.wSecond,
      now.wMilliseconds,
      GetCurrentProcessId(),
      GetCurrentThreadId(),
      level,
      buffer);
  OutputDebugStringA(final_buffer);

  if (!g_state.log_lock_ready) {
    return;
  }

  EnsureLogFileReady();
  if (g_state.log_file == nullptr) {
    return;
  }

  EnterCriticalSection(&g_state.log_lock);
  DWORD written = 0;
  WriteFile(g_state.log_file, final_buffer, static_cast<DWORD>(strlen(final_buffer)), &written, nullptr);
  FlushFileBuffers(g_state.log_file);
  LeaveCriticalSection(&g_state.log_lock);
}

void UpdateLastOperation(UINT vk, LONG rc, DWORD last_error) {
  InterlockedExchange(&g_state.last_vk, static_cast<LONG>(vk));
  InterlockedExchange(&g_state.last_result, rc);
  InterlockedExchange(&g_state.last_error, static_cast<LONG>(last_error));
}

void BuildSnapshotText(const char* reason, char* out, size_t capacity) {
  if (out == nullptr || capacity == 0) {
    return;
  }

  out[0] = '\0';
  size_t offset = 0;

  const uint32_t module_base = static_cast<uint32_t>(GetProcessImageBase());
  const uint32_t runtime_nwn_wndproc = RebaseAddress(kExpectedNwnWndProc);
  const uint32_t runtime_key_pre_dispatch = RebaseAddress(kExpectedKeyPreDispatch);
  const uint32_t runtime_gate90_accessor = RebaseAddress(kExpectedGate90Accessor);
  const uint32_t runtime_gate94_accessor = RebaseAddress(kExpectedGate94Accessor);
  const uint32_t runtime_gate98_accessor = RebaseAddress(kExpectedGate98Accessor);
  const uint32_t runtime_dispatcher_accessor = RebaseAddress(kExpectedDispatcherAccessor);
  const uint32_t runtime_dispatcher_thunk = RebaseAddress(kExpectedDispatcherThunk);
  const uint32_t runtime_dispatcher_slot0 = RebaseAddress(kExpectedDispatcherSlot0);
  const uint32_t runtime_quickbar_exec = RebaseAddress(kExpectedQuickbarExec);
  const uint32_t runtime_quickbar_page_select = RebaseAddress(kExpectedQuickbarPageSelect);
  const uint32_t runtime_quickbar_slot_dispatch = RebaseAddress(kExpectedQuickbarSlotDispatch);
  const uint32_t runtime_quickbar_vtable = RebaseAddress(kExpectedQuickbarVtable);
  const uint32_t runtime_object_by_id_resolver = RebaseAddress(kExpectedObjectByIdResolver);
  const uint32_t runtime_item_equipped_owner_resolver = RebaseAddress(kExpectedItemEquippedOwnerResolver);
  const uint32_t runtime_chat_send = RebaseAddress(kExpectedChatSend);
  const uint32_t runtime_chat_window_log = RebaseAddress(kExpectedChatWindowLog);
  const uint32_t runtime_app_object_resolver = RebaseAddress(kExpectedAppObjectResolver);
  const uint32_t runtime_current_player_resolver = RebaseAddress(kExpectedCurrentPlayerResolver);
  const uint32_t runtime_player_name_builder = RebaseAddress(kExpectedPlayerNameBuilder);
  const uint32_t runtime_nwn_string_destroy = RebaseAddress(kExpectedNwnStringDestroy);
  char character_name[kCharacterNameCapacity] = {};
  CopyStoredCharacterName(character_name, ARRAYSIZE(character_name));
  const LONG quickbar_slot_type = InterlockedCompareExchange(&g_state.quickbar_slot_type, 0, 0);
  const LONG quickbar_slot_case = QuickbarSlotTypeToCaseIndex(quickbar_slot_type);
  const uint32_t quickbar_item_mask_low =
      static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_item_mask_low, 0, 0));
  const uint32_t quickbar_item_mask_high =
      static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_item_mask_high, 0, 0));
  const uint32_t quickbar_equipped_mask_low =
      static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_equipped_mask_low, 0, 0));
  const uint32_t quickbar_equipped_mask_high =
      static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_equipped_mask_high, 0, 0));

  const HWND hwnd = g_state.hwnd;
  const uint32_t current_wndproc = (hwnd != nullptr && IsWindow(hwnd))
      ? static_cast<uint32_t>(GetWindowLongPtrA(hwnd, GWLP_WNDPROC))
      : 0;
  const uint32_t original_wndproc = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(g_state.original_wndproc));

  char class_name[128] = {};
  char window_title[256] = {};
  if (hwnd != nullptr && IsWindow(hwnd)) {
    GetClassNameA(hwnd, class_name, ARRAYSIZE(class_name));
    GetWindowTextA(hwnd, window_title, ARRAYSIZE(window_title));
  }

  const HWND foreground = GetForegroundWindow();

  const uint32_t app_holder = ReadAppHolderPointer();
  const uint32_t app_object = ReadAppObjectPointer();
  uint32_t app_inner = 0;
  uint32_t dispatcher_ptr = 0;
  uint32_t gate_90 = 0;
  uint32_t gate_94 = 0;
  uint32_t gate_98 = 0;
  if (app_object != 0) {
    app_inner = SafeReadPointer32(static_cast<uintptr_t>(app_object) + 4);
  }
  if (app_inner != 0) {
    dispatcher_ptr = SafeReadPointer32(static_cast<uintptr_t>(app_inner) + 0x24);
    gate_90 = SafeReadPointer32(static_cast<uintptr_t>(app_inner) + 0x90);
    gate_94 = SafeReadPointer32(static_cast<uintptr_t>(app_inner) + 0x94);
    gate_98 = SafeReadPointer32(static_cast<uintptr_t>(app_inner) + 0x98);
  }

  AppendFormat(out, capacity, &offset, "reason=%s\r\n", reason != nullptr ? reason : "snapshot");
  AppendFormat(out, capacity, &offset, "process: pid=%lu tid=%lu imageBase=0x%08X\r\n", GetCurrentProcessId(), GetCurrentThreadId(), module_base);
  AppendFormat(out, capacity, &offset, "hook: module=%s\r\n", g_state.module_path[0] != '\0' ? g_state.module_path : "<unavailable>");
  AppendFormat(out, capacity, &offset, "hook: log=%s\r\n", g_state.log_path[0] != '\0' ? g_state.log_path : "<unavailable>");
  AppendFormat(out, capacity, &offset, "hook: installed=%ld logLevel=%ld pendingBusy=%ld pipeState=%ld pipeErr=%ld\r\n",
      InterlockedCompareExchange(&g_state.installed, 0, 0),
      InterlockedCompareExchange(&g_state.log_level, 0, 0),
      InterlockedCompareExchange(&g_state.pending.busy, 0, 0),
      InterlockedCompareExchange(&g_state.pipe_state, 0, 0),
      InterlockedCompareExchange(&g_state.pipe_thread_error, 0, 0));
  AppendFormat(out, capacity, &offset, "window: hwnd=0x%08X thread=%lu visible=%d class=%s title=%s\r\n",
      static_cast<uint32_t>(reinterpret_cast<uintptr_t>(hwnd)),
      g_state.window_thread_id,
      hwnd != nullptr && IsWindowVisible(hwnd),
      class_name[0] != '\0' ? class_name : "<unknown>",
      window_title[0] != '\0' ? window_title : "<untitled>");
  AppendFormat(out, capacity, &offset, "window: foreground=0x%08X matches=%d currentWndProc=0x%08X hookWndProc=0x%08X originalWndProc=0x%08X\r\n",
      static_cast<uint32_t>(reinterpret_cast<uintptr_t>(foreground)),
      foreground == hwnd,
      current_wndproc,
      static_cast<uint32_t>(reinterpret_cast<uintptr_t>(&SimKeysWndProc)),
      original_wndproc);
  AppendFormat(out, capacity, &offset, "expected: wndProc=0x%08X keyPreDispatch=0x%08X gate90=0x%08X gate94=0x%08X gate98=0x%08X dispatcherAccessor=0x%08X dispatcherThunk=0x%08X dispatcherSlot0=0x%08X\r\n",
      runtime_nwn_wndproc,
      runtime_key_pre_dispatch,
      runtime_gate90_accessor,
      runtime_gate94_accessor,
      runtime_gate98_accessor,
      runtime_dispatcher_accessor,
      runtime_dispatcher_thunk,
      runtime_dispatcher_slot0);
  AppendFormat(out, capacity, &offset, "engine: appGlobalSlot=0x%08X appHolder=0x%08X appObject=0x%08X appInner=0x%08X dispatcher=0x%08X gate90Value=0x%08X gate94Value=0x%08X gate98Value=0x%08X\r\n",
      static_cast<uint32_t>(kAppGlobalSlotAddress),
      app_holder,
      app_object,
      app_inner,
      dispatcher_ptr,
      gate_90,
      gate_94,
      gate_98);
  AppendFormat(out, capacity, &offset, "quickbar: exec=0x%08X pageSelect=0x%08X slotDispatch=0x%08X panelVtable=0x%08X objectById=0x%08X equipOwner=0x%08X execTrace=%ld slotTrace=%ld capturedThis=0x%08X page=%ld capturedSlot=%ld slotPtr=0x%08X slotType=%ld slotCase=%ld calls=%ld scanAttempts=%ld scanHits=%ld itemMask=0x%08X%08X equippedMask=0x%08X%08X\r\n",
      runtime_quickbar_exec,
      runtime_quickbar_page_select,
      runtime_quickbar_slot_dispatch,
      runtime_quickbar_vtable,
      runtime_object_by_id_resolver,
      runtime_item_equipped_owner_resolver,
      InterlockedCompareExchange(&g_state.quickbar_trace_installed, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_slot_trace_installed, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_this, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_page, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_slot, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_slot_ptr, 0, 0),
      quickbar_slot_type,
      quickbar_slot_case,
      InterlockedCompareExchange(&g_state.quickbar_calls, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_scan_attempts, 0, 0),
      InterlockedCompareExchange(&g_state.quickbar_scan_hits, 0, 0),
      quickbar_item_mask_high,
      quickbar_item_mask_low,
      quickbar_equipped_mask_high,
      quickbar_equipped_mask_low);
  AppendFormat(out, capacity, &offset, "chat: send=0x%08X windowLog=0x%08X trace=%ld queued=%ld nextWrite=%ld latestSeq=%ld lastMode=%ld lastRc=%ld lastErr=%ld\r\n",
      runtime_chat_send,
      runtime_chat_window_log,
      InterlockedCompareExchange(&g_state.chat_trace_installed, 0, 0),
      InterlockedCompareExchange(&g_state.chat_count, 0, 0),
      InterlockedCompareExchange(&g_state.chat_write_index, 0, 0),
      InterlockedCompareExchange(&g_state.chat_sequence, 0, 0),
      InterlockedCompareExchange(&g_state.last_chat_mode, 0, 0),
      InterlockedCompareExchange(&g_state.last_chat_result, 0, 0),
      InterlockedCompareExchange(&g_state.last_chat_error, 0, 0));
  AppendFormat(out, capacity, &offset, "identityPath: appObjectResolver=0x%08X currentPlayerResolver=0x%08X nameBuilder=0x%08X stringDestroy=0x%08X\r\n",
      runtime_app_object_resolver,
      runtime_current_player_resolver,
      runtime_player_name_builder,
      runtime_nwn_string_destroy);
  AppendFormat(out, capacity, &offset, "identity: player=0x%08X name=%s refreshes=%ld err=%ld\r\n",
      static_cast<uint32_t>(InterlockedCompareExchange(&g_state.player_object, 0, 0)),
      character_name[0] != '\0' ? character_name : "<unknown>",
      InterlockedCompareExchange(&g_state.identity_refresh_count, 0, 0),
      InterlockedCompareExchange(&g_state.identity_error, 0, 0));
  AppendFormat(out, capacity, &offset, "last: vk=0x%08X rc=%ld err=%ld requestId=%ld\r\n",
      InterlockedCompareExchange(&g_state.last_vk, 0, 0),
      InterlockedCompareExchange(&g_state.last_result, 0, 0),
      InterlockedCompareExchange(&g_state.last_error, 0, 0),
      InterlockedCompareExchange(&g_state.pending.request_id, 0, 0));
}

void LogSnapshot(int level, const char* reason) {
  if (level > InterlockedCompareExchange(&g_state.log_level, 0, 0)) {
    return;
  }

  char snapshot[4096] = {};
  BuildSnapshotText(reason, snapshot, sizeof(snapshot));
  LogMessage(level, "%s", snapshot);
}

LPARAM BuildKeyDownLParam(UINT vk) {
  const UINT scan_code = MapVirtualKeyA(vk, MAPVK_VK_TO_VSC);
  return static_cast<LPARAM>(1u | (scan_code << 16));
}

LPARAM BuildKeyUpLParam(UINT vk) {
  const UINT scan_code = MapVirtualKeyA(vk, MAPVK_VK_TO_VSC);
  return static_cast<LPARAM>(1u | (scan_code << 16) | (1u << 30) | (1u << 31));
}

LRESULT CallKeyPreDispatch(HWND hwnd, UINT vk) {
#if defined(_M_IX86)
  typedef LRESULT (WINAPI* KeyPreDispatchFn)(HWND hwnd, WPARAM wparam, LPARAM lparam);
#else
  typedef LRESULT (WINAPI* KeyPreDispatchFn)(HWND hwnd, WPARAM wparam, LPARAM lparam);
#endif
  const KeyPreDispatchFn fn = reinterpret_cast<KeyPreDispatchFn>(RebaseAddress(kExpectedKeyPreDispatch));
  return fn(hwnd, static_cast<WPARAM>(vk), BuildKeyDownLParam(vk));
}

UINT SlotToVirtualKey(int slot) {
  if (slot < 1 || slot > 12) {
    return 0;
  }
  return static_cast<UINT>(VK_F1 + (slot - 1));
}

LONG CallChatSendDirect(const char* text, int mode) {
  struct NwnStringRef {
    char* text;
    int32_t length;
  };

  if (text == nullptr || text[0] == '\0') {
    SetLastError(ERROR_INVALID_PARAMETER);
    return 0;
  }

  typedef void (__cdecl* ChatSendFn)(const void* text_object, int mode);
  const ChatSendFn fn = reinterpret_cast<ChatSendFn>(RebaseAddress(kExpectedChatSend));

  NwnStringRef message = {};
  message.text = const_cast<char*>(text);
  message.length = static_cast<int32_t>(strnlen(text, kPendingChatCapacity));
  fn(&message, mode);
  return 1;
}

BOOL ResolveCurrentCharacterIdentityOnWindowThread(DWORD* out_error) {
  struct NwnStringRef {
    char* text;
    int32_t length;
  };

  typedef void* (__thiscall* ResolveAppObjectFn)(void* app_holder);
  typedef void* (__thiscall* ResolveCurrentPlayerFn)(void* app_object);
  typedef NwnStringRef* (__thiscall* BuildPlayerNameFn)(void* player_object, NwnStringRef* out_name);
  typedef void (__thiscall* DestroyNwnStringFn)(NwnStringRef* text_object);

  DWORD last_error = ERROR_SUCCESS;
  void* app_object = nullptr;
  void* player_object = nullptr;
  NwnStringRef name = {};
  char local_name[kCharacterNameCapacity] = {};

  const uint32_t app_holder = ReadAppHolderPointer();
  if (app_holder == 0) {
    last_error = ERROR_NOT_FOUND;
  } else {
    const ResolveAppObjectFn resolve_app_object =
        reinterpret_cast<ResolveAppObjectFn>(RebaseAddress(kExpectedAppObjectResolver));
    const ResolveCurrentPlayerFn resolve_current_player =
        reinterpret_cast<ResolveCurrentPlayerFn>(RebaseAddress(kExpectedCurrentPlayerResolver));
    const BuildPlayerNameFn build_player_name =
        reinterpret_cast<BuildPlayerNameFn>(RebaseAddress(kExpectedPlayerNameBuilder));
    const DestroyNwnStringFn destroy_nwn_string =
        reinterpret_cast<DestroyNwnStringFn>(RebaseAddress(kExpectedNwnStringDestroy));

    __try {
      app_object = resolve_app_object(reinterpret_cast<void*>(app_holder));
      if (app_object != nullptr) {
        player_object = resolve_current_player(app_object);
      }
      if (player_object != nullptr) {
        build_player_name(player_object, &name);
      } else {
        last_error = ERROR_NOT_FOUND;
      }
      if (last_error == ERROR_SUCCESS && (name.text == nullptr || name.text[0] == '\0')) {
        last_error = ERROR_NOT_FOUND;
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      last_error = static_cast<DWORD>(GetExceptionCode());
    }

    if (last_error == ERROR_SUCCESS) {
      if (name.text != nullptr && name.text[0] != '\0') {
        strncpy_s(local_name, sizeof(local_name), name.text, _TRUNCATE);
      } else {
        last_error = ERROR_NOT_FOUND;
      }
    }

    if (name.text != nullptr) {
      __try {
        destroy_nwn_string(&name);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        if (last_error == ERROR_SUCCESS) {
          last_error = static_cast<DWORD>(GetExceptionCode());
        }
      }
    }
  }

  StoreCharacterName(last_error == ERROR_SUCCESS ? local_name : "");
  InterlockedExchange(&g_state.player_object, static_cast<LONG>(reinterpret_cast<uintptr_t>(player_object)));
  InterlockedExchange(&g_state.identity_error, static_cast<LONG>(last_error));
  InterlockedIncrement(&g_state.identity_refresh_count);
  UpdateQuickbarItemMasksOnWindowThread();

  if (last_error == ERROR_SUCCESS) {
    LogMessage(
        kLogDebug,
        "identity refresh resolved holder=0x%08X app=0x%08X player=0x%08X namePtr=0x%08X nameLen=%ld name=%s",
        app_holder,
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(app_object)),
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(player_object)),
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(name.text)),
        static_cast<long>(name.length),
        local_name);
  } else {
    LogMessage(
        kLogDebug,
        "identity refresh failed holder=0x%08X app=0x%08X player=0x%08X namePtr=0x%08X nameLen=%ld err=%lu",
        app_holder,
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(app_object)),
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(player_object)),
        static_cast<unsigned int>(reinterpret_cast<uintptr_t>(name.text)),
        static_cast<long>(name.length),
        static_cast<unsigned long>(last_error));
  }

  if (out_error != nullptr) {
    *out_error = last_error;
  }
  return last_error == ERROR_SUCCESS;
}

BOOL ReadExact(HANDLE handle, void* buffer, DWORD size) {
  BYTE* out = static_cast<BYTE*>(buffer);
  DWORD total = 0;
  while (total < size) {
    DWORD read_now = 0;
    if (!ReadFile(handle, out + total, size - total, &read_now, nullptr) || read_now == 0) {
      return FALSE;
    }
    total += read_now;
  }
  return TRUE;
}

BOOL WriteExact(HANDLE handle, const void* buffer, DWORD size) {
  const BYTE* in = static_cast<const BYTE*>(buffer);
  DWORD total = 0;
  while (total < size) {
    DWORD wrote_now = 0;
    if (!WriteFile(handle, in + total, size - total, &wrote_now, nullptr) || wrote_now == 0) {
      return FALSE;
    }
    total += wrote_now;
  }
  return TRUE;
}

BOOL WriteResponse(HANDLE pipe, uint32_t op, const void* payload, uint32_t payload_size) {
  PipeHeader header = {op, payload_size};
  if (!WriteExact(pipe, &header, sizeof(header))) {
    return FALSE;
  }
  if (payload_size == 0) {
    return TRUE;
  }
  return WriteExact(pipe, payload, payload_size);
}

struct FindWindowContext {
  DWORD process_id;
  HWND visible_hwnd;
  HWND fallback_hwnd;
  DWORD visible_thread_id;
  DWORD fallback_thread_id;
};

BOOL CALLBACK EnumWindowsProc(HWND hwnd, LPARAM lparam) {
  FindWindowContext* ctx = reinterpret_cast<FindWindowContext*>(lparam);

  DWORD process_id = 0;
  DWORD thread_id = GetWindowThreadProcessId(hwnd, &process_id);
  if (process_id != ctx->process_id) {
    return TRUE;
  }
  if (GetWindow(hwnd, GW_OWNER) != nullptr) {
    return TRUE;
  }

  if (ctx->fallback_hwnd == nullptr) {
    ctx->fallback_hwnd = hwnd;
    ctx->fallback_thread_id = thread_id;
  }

  if (IsWindowVisible(hwnd)) {
    ctx->visible_hwnd = hwnd;
    ctx->visible_thread_id = thread_id;
    return FALSE;
  }

  return TRUE;
}

BOOL FindGameWindow(HWND* out_hwnd, DWORD* out_thread_id) {
  FindWindowContext ctx = {};
  ctx.process_id = GetCurrentProcessId();

  if (!EnumWindows(EnumWindowsProc, reinterpret_cast<LPARAM>(&ctx))) {
    // Enumeration stopped early after finding a visible window.
  }

  if (ctx.visible_hwnd != nullptr) {
    *out_hwnd = ctx.visible_hwnd;
    *out_thread_id = ctx.visible_thread_id;
    return TRUE;
  }
  if (ctx.fallback_hwnd != nullptr) {
    *out_hwnd = ctx.fallback_hwnd;
    *out_thread_id = ctx.fallback_thread_id;
    return TRUE;
  }

  *out_hwnd = nullptr;
  *out_thread_id = 0;
  return FALSE;
}

LRESULT CALLBACK SimKeysWndProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
  if (message == kMsgTriggerVk) {
    const LONG request_id = static_cast<LONG>(lparam);
    const UINT vk = static_cast<UINT>(wparam);

    if (request_id != InterlockedCompareExchange(&g_state.pending.request_id, 0, 0)) {
      return 0;
    }

    DWORD last_error = ERROR_SUCCESS;
    LRESULT rc = 0;
    LRESULT aux_rc = 0;
    LONG dispatch_path = 0;

    __try {
      const int slot_index = static_cast<int>(vk) - static_cast<int>(VK_F1);
      if (slot_index >= 0 && slot_index < 12 && InterlockedCompareExchange(&g_state.quickbar_this, 0, 0) != 0) {
        rc = CallQuickbarExecDirect(slot_index);
        dispatch_path = 2;
        LogMessage(
            kLogDebug,
            "dispatched vk=0x%02X through quickbar exec slot=%d capturedThis=0x%08X rc=%ld",
            vk,
            slot_index,
            InterlockedCompareExchange(&g_state.quickbar_this, 0, 0),
            static_cast<long>(rc));
      } else {
        rc = CallKeyPreDispatch(hwnd, vk);
        if (g_state.original_wndproc != nullptr) {
          aux_rc = CallWindowProcA(g_state.original_wndproc, hwnd, WM_KEYUP, static_cast<WPARAM>(vk), BuildKeyUpLParam(vk));
        }
        dispatch_path = 1;
        LogMessage(
            kLogDebug,
            "dispatched vk=0x%02X through keyPreDispatch rc=%ld and keyUp rc=%ld",
            vk,
            static_cast<long>(rc),
            static_cast<long>(aux_rc));
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      last_error = static_cast<DWORD>(GetExceptionCode());
      rc = 0;
      aux_rc = 0;
      dispatch_path = 0;
      LogMessage(kLogError, "keyPreDispatch raised exception vk=0x%02X code=0x%08lX", vk, static_cast<unsigned long>(last_error));
    }

    InterlockedExchange(&g_state.pending.vk, static_cast<LONG>(vk));
    InterlockedExchange(&g_state.pending.result, static_cast<LONG>(rc));
    InterlockedExchange(&g_state.pending.aux_result, static_cast<LONG>(aux_rc));
    InterlockedExchange(&g_state.pending.dispatch_path, dispatch_path);
    InterlockedExchange(&g_state.pending.last_error, static_cast<LONG>(last_error));
    UpdateLastOperation(vk, static_cast<LONG>(rc), last_error);
    SetEvent(g_state.pending.event);
    return 0;
  }

  if (message == kMsgTriggerPageSlot) {
    const LONG request_id = static_cast<LONG>(lparam);
    if (request_id != InterlockedCompareExchange(&g_state.pending.request_id, 0, 0)) {
      return 0;
    }

    const int requested_page = static_cast<int>(LOWORD(static_cast<DWORD_PTR>(wparam)));
    const int slot_index = static_cast<int>(HIWORD(static_cast<DWORD_PTR>(wparam)));
    const UINT vk = SlotToVirtualKey(slot_index + 1);

    DWORD last_error = ERROR_SUCCESS;
    LRESULT rc = 0;
    LRESULT aux_rc = -1;
    LONG dispatch_path = 0;

    __try {
      if (requested_page < 0 || requested_page >= kQuickbarPageCount || slot_index < 0 || slot_index >= kQuickbarSlotCount) {
        last_error = ERROR_INVALID_PARAMETER;
      } else {
        LONG quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
        if (quickbar_this == 0) {
          DiscoverQuickbarPanelByScan("page-slot-trigger");
          quickbar_this = InterlockedCompareExchange(&g_state.quickbar_this, 0, 0);
        }

        if (quickbar_this == 0) {
          last_error = ERROR_NOT_FOUND;
        } else {
          LONG original_page = ResolveQuickbarPageIndex(static_cast<uint32_t>(quickbar_this));
          if (original_page < 0) {
            const LONG cached_page = InterlockedCompareExchange(&g_state.quickbar_page, 0, 0);
            if (cached_page >= 0 && cached_page < kQuickbarPageCount) {
              original_page = cached_page;
            }
          }
          aux_rc = original_page;

          LONG resolved_target_page = original_page;
          LONG page_after_exec = original_page;
          LONG final_page = original_page;
          const bool restore_needed = original_page >= 0 && original_page != requested_page;

          if (original_page != requested_page) {
            if (!CallQuickbarPageSelectDirect(requested_page, &resolved_target_page)) {
              last_error = GetLastError();
              LogMessage(
                  kLogError,
                  "quickbar page-slot request could not switch to page=%d slot=%d err=%lu",
                  requested_page,
                  slot_index,
                  static_cast<unsigned long>(last_error));
            }
          }

          if (last_error == ERROR_SUCCESS) {
            rc = CallQuickbarExecDirect(slot_index);
            if (rc == 0) {
              last_error = GetLastError();
            } else {
              dispatch_path = 3;
            }
            page_after_exec = ResolveQuickbarPageIndex(static_cast<uint32_t>(quickbar_this));
            final_page = page_after_exec;
          }

          if (dispatch_path == 3 && restore_needed) {
            LONG restored_page = -1;
            if (!CallQuickbarPageSelectDirect(original_page, &restored_page)) {
              const DWORD restore_error = GetLastError();
              if (last_error == ERROR_SUCCESS) {
                last_error = restore_error;
              }
              LogMessage(
                  kLogError,
                  "quickbar page-slot request restore failed original=%ld requested=%d slot=%d err=%lu",
                  original_page,
                  requested_page,
                  slot_index,
                  static_cast<unsigned long>(restore_error));
            } else {
              final_page = restored_page;
            }
          }

          LogMessage(
              kLogDebug,
              "dispatched quickbar page-slot requestPage=%d slot=%d vk=0x%02X panel=0x%08X originalPage=%ld targetPage=%ld pageAfterExec=%ld finalPage=%ld rc=%ld err=%lu",
              requested_page,
              slot_index,
              vk,
              static_cast<unsigned int>(quickbar_this),
              original_page,
              resolved_target_page,
              page_after_exec,
              final_page,
              static_cast<long>(rc),
              static_cast<unsigned long>(last_error));
        }
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      last_error = static_cast<DWORD>(GetExceptionCode());
      rc = 0;
      aux_rc = -1;
      dispatch_path = 0;
      LogMessage(
          kLogError,
          "quickbar page-slot dispatch raised exception page=%d slot=%d code=0x%08lX",
          requested_page,
          slot_index,
          static_cast<unsigned long>(last_error));
    }

    InterlockedExchange(&g_state.pending.vk, static_cast<LONG>(vk));
    InterlockedExchange(&g_state.pending.result, static_cast<LONG>(rc));
    InterlockedExchange(&g_state.pending.aux_result, static_cast<LONG>(aux_rc));
    InterlockedExchange(&g_state.pending.dispatch_path, dispatch_path);
    InterlockedExchange(&g_state.pending.last_error, static_cast<LONG>(last_error));
    UpdateLastOperation(vk, static_cast<LONG>(rc), last_error);
    SetEvent(g_state.pending.event);
    return 0;
  }

  if (message == kMsgSendChat) {
    const LONG request_id = static_cast<LONG>(lparam);
    if (request_id != InterlockedCompareExchange(&g_state.pending_chat.request_id, 0, 0)) {
      return 0;
    }

    const LONG mode = InterlockedCompareExchange(&g_state.pending_chat.mode, 0, 0);
    const char* text = g_state.pending_chat.text;
    DWORD last_error = ERROR_SUCCESS;
    LONG rc = 0;

    __try {
      rc = CallChatSendDirect(text, static_cast<int>(mode));
      LogMessage(kLogInfo, "chat send dispatched mode=%ld text=%s", mode, text);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      last_error = static_cast<DWORD>(GetExceptionCode());
      rc = 0;
      LogMessage(kLogError, "chat send raised exception mode=%ld code=0x%08lX text=%s", mode, static_cast<unsigned long>(last_error), text);
    }

    InterlockedExchange(&g_state.pending_chat.result, rc);
    InterlockedExchange(&g_state.pending_chat.last_error, static_cast<LONG>(last_error));
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, rc);
    InterlockedExchange(&g_state.last_chat_error, static_cast<LONG>(last_error));
    SetEvent(g_state.pending_chat.event);
    return 0;
  }

  if (message == kMsgRefreshIdentity) {
    const LONG request_id = static_cast<LONG>(lparam);
    if (request_id != InterlockedCompareExchange(&g_state.pending_identity.request_id, 0, 0)) {
      return 0;
    }

    DWORD last_error = ERROR_SUCCESS;
    ResolveCurrentCharacterIdentityOnWindowThread(&last_error);
    InterlockedExchange(&g_state.pending_identity.last_error, static_cast<LONG>(last_error));
    SetEvent(g_state.pending_identity.event);
    return 0;
  }

  if (g_state.original_wndproc != nullptr) {
    return CallWindowProcA(g_state.original_wndproc, hwnd, message, wparam, lparam);
  }
  return DefWindowProcA(hwnd, message, wparam, lparam);
}

BOOL EnsureHookInstalled() {
  if (!g_state.lock_ready) {
    SetLastError(ERROR_INVALID_STATE);
    return FALSE;
  }

  EnterCriticalSection(&g_state.lock);

  if (g_state.hwnd != nullptr && IsWindow(g_state.hwnd)) {
    const LONG_PTR current_hook = GetWindowLongPtrA(g_state.hwnd, GWLP_WNDPROC);
    if (InterlockedCompareExchange(&g_state.installed, 0, 0) != 0 &&
        current_hook == reinterpret_cast<LONG_PTR>(&SimKeysWndProc) &&
        g_state.original_wndproc != nullptr) {
      LeaveCriticalSection(&g_state.lock);
      return TRUE;
    }
  }

  HWND hwnd = nullptr;
  DWORD thread_id = 0;
  if (!FindGameWindow(&hwnd, &thread_id)) {
    LeaveCriticalSection(&g_state.lock);
    SetLastError(ERROR_FILE_NOT_FOUND);
    UpdateLastOperation(0, 0, ERROR_FILE_NOT_FOUND);
    LogMessage(kLogError, "could not find the NWN game window in pid=%lu", GetCurrentProcessId());
    return FALSE;
  }

  const LONG_PTR current_proc = GetWindowLongPtrA(hwnd, GWLP_WNDPROC);
  if (current_proc == 0) {
    const DWORD gle = GetLastError();
    LeaveCriticalSection(&g_state.lock);
    SetLastError(gle);
    UpdateLastOperation(0, 0, gle);
    LogMessage(kLogError, "GetWindowLongPtrA(GWLP_WNDPROC) failed gle=%lu", gle);
    return FALSE;
  }

  SetLastError(ERROR_SUCCESS);
  const LONG_PTR previous_proc = SetWindowLongPtrA(hwnd, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(&SimKeysWndProc));
  const DWORD set_wndproc_error = GetLastError();
  if (previous_proc == 0 && set_wndproc_error != ERROR_SUCCESS) {
    LeaveCriticalSection(&g_state.lock);
    SetLastError(set_wndproc_error);
    UpdateLastOperation(0, 0, set_wndproc_error);
    LogMessage(kLogError, "SetWindowLongPtrA(GWLP_WNDPROC) failed gle=%lu", set_wndproc_error);
    return FALSE;
  }

  g_state.hwnd = hwnd;
  g_state.window_thread_id = thread_id;
  g_state.original_wndproc = reinterpret_cast<WNDPROC>(previous_proc == 0 ? current_proc : previous_proc);
  InterlockedExchange(&g_state.installed, 1);

  LeaveCriticalSection(&g_state.lock);

  LogMessage(
      kLogInfo,
      "installed window hook hwnd=0x%08X current=0x%08X original=0x%08X thread=%lu expected_nwn_wndproc=0x%08X",
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(g_state.hwnd)),
      static_cast<unsigned int>(current_proc),
      static_cast<unsigned int>(reinterpret_cast<uintptr_t>(g_state.original_wndproc)),
      g_state.window_thread_id,
      RebaseAddress(kExpectedNwnWndProc));
  LogSnapshot(kLogDebug, "after-hook-install");

  return TRUE;
}

BOOL TriggerVirtualKey(UINT vk, LONG* out_rc, DWORD* out_error) {
  if (vk == 0) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_INVALID_PARAMETER;
    }
    UpdateLastOperation(0, 0, ERROR_INVALID_PARAMETER);
    return FALSE;
  }

  if (vk >= VK_F1 && vk <= VK_F12 && InterlockedCompareExchange(&g_state.quickbar_this, 0, 0) == 0) {
    DiscoverQuickbarPanelByScan("trigger");
  }

  if (!EnsureHookInstalled()) {
    const DWORD gle = GetLastError();
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    return FALSE;
  }

  if (InterlockedCompareExchange(&g_state.pending.busy, 1, 0) != 0) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_BUSY;
    }
    UpdateLastOperation(vk, 0, ERROR_BUSY);
    LogMessage(kLogError, "trigger rejected for vk=0x%02X because a previous dispatch is still in flight", vk);
    return FALSE;
  }

  const LONG request_id = InterlockedIncrement(&g_state.pending.sequence_seed);
  InterlockedExchange(&g_state.pending.request_id, request_id);
  InterlockedExchange(&g_state.pending.vk, static_cast<LONG>(vk));
  InterlockedExchange(&g_state.pending.result, 0);
  InterlockedExchange(&g_state.pending.aux_result, 0);
  InterlockedExchange(&g_state.pending.dispatch_path, 0);
  InterlockedExchange(&g_state.pending.last_error, static_cast<LONG>(ERROR_IO_PENDING));
  ResetEvent(g_state.pending.event);
  LogSnapshot(kLogDebug, "before-trigger");

  if (!PostMessageA(g_state.hwnd, kMsgTriggerVk, static_cast<WPARAM>(vk), static_cast<LPARAM>(request_id))) {
    const DWORD gle = GetLastError();
    InterlockedExchange(&g_state.pending.busy, 0);
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    UpdateLastOperation(vk, 0, gle);
    LogMessage(kLogError, "PostMessageA(custom trigger) failed vk=0x%02X gle=%lu", vk, gle);
    return FALSE;
  }

  const DWORD wait_rc = WaitForSingleObject(g_state.pending.event, kDispatchTimeoutMs);
  const LONG result = InterlockedCompareExchange(&g_state.pending.result, 0, 0);
  const LONG aux_result = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
  const LONG dispatch_path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
  const DWORD last_error = static_cast<DWORD>(InterlockedCompareExchange(&g_state.pending.last_error, 0, 0));
  InterlockedExchange(&g_state.pending.busy, 0);

  if (wait_rc != WAIT_OBJECT_0) {
    const DWORD gle = (wait_rc == WAIT_TIMEOUT) ? WAIT_TIMEOUT : GetLastError();
    if (out_rc != nullptr) {
      *out_rc = result;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    UpdateLastOperation(vk, result, gle);
    LogMessage(kLogError, "dispatch wait failed vk=0x%02X wait_rc=%lu gle=%lu path=%ld rc=%ld aux=%ld", vk, wait_rc, gle, dispatch_path, result, aux_result);
    LogSnapshot(kLogDebug, "after-trigger-wait-failure");
    return FALSE;
  }

  if (out_rc != nullptr) {
    *out_rc = result;
  }
  if (out_error != nullptr) {
    *out_error = last_error;
  }
  LogMessage(kLogDebug, "dispatch completed vk=0x%02X path=%ld rc=%ld aux=%ld err=%lu", vk, dispatch_path, result, aux_result, static_cast<unsigned long>(last_error));
  LogSnapshot(kLogDebug, "after-trigger");

  return last_error == ERROR_SUCCESS;
}

BOOL TriggerQuickbarPageSlot(int page_index, int slot, LONG* out_rc, DWORD* out_error) {
  if (page_index < 0 || page_index >= kQuickbarPageCount || slot < 1 || slot > kQuickbarSlotCount) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_INVALID_PARAMETER;
    }
    UpdateLastOperation(0, 0, ERROR_INVALID_PARAMETER);
    return FALSE;
  }

  if (InterlockedCompareExchange(&g_state.quickbar_this, 0, 0) == 0) {
    DiscoverQuickbarPanelByScan("page-slot-trigger");
  }

  if (!EnsureHookInstalled()) {
    const DWORD gle = GetLastError();
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    return FALSE;
  }

  if (InterlockedCompareExchange(&g_state.pending.busy, 1, 0) != 0) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_BUSY;
    }
    UpdateLastOperation(0, 0, ERROR_BUSY);
    LogMessage(
        kLogError,
        "page-slot trigger rejected for page=%d slot=%d because a previous dispatch is still in flight",
        page_index,
        slot);
    return FALSE;
  }

  const UINT vk = SlotToVirtualKey(slot);
  const LONG request_id = InterlockedIncrement(&g_state.pending.sequence_seed);
  InterlockedExchange(&g_state.pending.request_id, request_id);
  InterlockedExchange(&g_state.pending.vk, static_cast<LONG>(vk));
  InterlockedExchange(&g_state.pending.result, 0);
  InterlockedExchange(&g_state.pending.aux_result, 0);
  InterlockedExchange(&g_state.pending.dispatch_path, 0);
  InterlockedExchange(&g_state.pending.last_error, static_cast<LONG>(ERROR_IO_PENDING));
  ResetEvent(g_state.pending.event);
  LogSnapshot(kLogDebug, "before-page-slot-trigger");

  const WPARAM packed = static_cast<WPARAM>(MAKELONG(page_index, slot - 1));
  if (!PostMessageA(g_state.hwnd, kMsgTriggerPageSlot, packed, static_cast<LPARAM>(request_id))) {
    const DWORD gle = GetLastError();
    InterlockedExchange(&g_state.pending.busy, 0);
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    UpdateLastOperation(vk, 0, gle);
    LogMessage(
        kLogError,
        "PostMessageA(page-slot trigger) failed page=%d slot=%d vk=0x%02X gle=%lu",
        page_index,
        slot,
        vk,
        static_cast<unsigned long>(gle));
    return FALSE;
  }

  const DWORD wait_rc = WaitForSingleObject(g_state.pending.event, kDispatchTimeoutMs);
  const LONG result = InterlockedCompareExchange(&g_state.pending.result, 0, 0);
  const LONG aux_result = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
  const LONG dispatch_path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
  const DWORD last_error = static_cast<DWORD>(InterlockedCompareExchange(&g_state.pending.last_error, 0, 0));
  InterlockedExchange(&g_state.pending.busy, 0);

  if (wait_rc != WAIT_OBJECT_0) {
    const DWORD gle = (wait_rc == WAIT_TIMEOUT) ? WAIT_TIMEOUT : GetLastError();
    if (out_rc != nullptr) {
      *out_rc = result;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    UpdateLastOperation(vk, result, gle);
    LogMessage(
        kLogError,
        "page-slot dispatch wait failed page=%d slot=%d vk=0x%02X wait_rc=%lu gle=%lu path=%ld rc=%ld aux=%ld",
        page_index,
        slot,
        vk,
        static_cast<unsigned long>(wait_rc),
        static_cast<unsigned long>(gle),
        dispatch_path,
        result,
        aux_result);
    LogSnapshot(kLogDebug, "after-page-slot-trigger-wait-failure");
    return FALSE;
  }

  if (out_rc != nullptr) {
    *out_rc = result;
  }
  if (out_error != nullptr) {
    *out_error = last_error;
  }
  LogMessage(
      kLogDebug,
      "page-slot dispatch completed page=%d slot=%d vk=0x%02X path=%ld rc=%ld aux=%ld err=%lu",
      page_index,
      slot,
      vk,
      dispatch_path,
      result,
      aux_result,
      static_cast<unsigned long>(last_error));
  LogSnapshot(kLogDebug, "after-page-slot-trigger");

  return dispatch_path == 3;
}

BOOL TriggerChatMessage(const char* text, int mode, LONG* out_rc, DWORD* out_error) {
  if (text == nullptr || text[0] == '\0') {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_INVALID_PARAMETER;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, 0);
    InterlockedExchange(&g_state.last_chat_error, ERROR_INVALID_PARAMETER);
    return FALSE;
  }

  const size_t text_length = strnlen(text, kPendingChatCapacity);
  if (text_length >= kPendingChatCapacity - 1) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_BUFFER_OVERFLOW;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, 0);
    InterlockedExchange(&g_state.last_chat_error, ERROR_BUFFER_OVERFLOW);
    return FALSE;
  }

  if (!EnsureHookInstalled()) {
    const DWORD gle = GetLastError();
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, 0);
    InterlockedExchange(&g_state.last_chat_error, static_cast<LONG>(gle));
    return FALSE;
  }

  if (InterlockedCompareExchange(&g_state.pending_chat.busy, 1, 0) != 0) {
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = ERROR_BUSY;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, 0);
    InterlockedExchange(&g_state.last_chat_error, ERROR_BUSY);
    LogMessage(kLogError, "chat send rejected because a previous chat dispatch is still in flight");
    return FALSE;
  }

  const LONG request_id = InterlockedIncrement(&g_state.pending_chat.sequence_seed);
  strncpy_s(g_state.pending_chat.text, sizeof(g_state.pending_chat.text), text, _TRUNCATE);
  InterlockedExchange(&g_state.pending_chat.request_id, request_id);
  InterlockedExchange(&g_state.pending_chat.mode, mode);
  InterlockedExchange(&g_state.pending_chat.result, 0);
  InterlockedExchange(&g_state.pending_chat.last_error, ERROR_IO_PENDING);
  ResetEvent(g_state.pending_chat.event);
  LogMessage(kLogDebug, "queueing chat send request mode=%d text=%s", mode, text);

  if (!PostMessageA(g_state.hwnd, kMsgSendChat, 0, static_cast<LPARAM>(request_id))) {
    const DWORD gle = GetLastError();
    InterlockedExchange(&g_state.pending_chat.busy, 0);
    if (out_rc != nullptr) {
      *out_rc = 0;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, 0);
    InterlockedExchange(&g_state.last_chat_error, static_cast<LONG>(gle));
    LogMessage(kLogError, "PostMessageA(chat trigger) failed gle=%lu", gle);
    return FALSE;
  }

  const DWORD wait_rc = WaitForSingleObject(g_state.pending_chat.event, kDispatchTimeoutMs);
  const LONG result = InterlockedCompareExchange(&g_state.pending_chat.result, 0, 0);
  const DWORD last_error = static_cast<DWORD>(InterlockedCompareExchange(&g_state.pending_chat.last_error, 0, 0));
  InterlockedExchange(&g_state.pending_chat.busy, 0);

  if (wait_rc != WAIT_OBJECT_0) {
    const DWORD gle = (wait_rc == WAIT_TIMEOUT) ? WAIT_TIMEOUT : GetLastError();
    if (out_rc != nullptr) {
      *out_rc = result;
    }
    if (out_error != nullptr) {
      *out_error = gle;
    }
    InterlockedExchange(&g_state.last_chat_mode, mode);
    InterlockedExchange(&g_state.last_chat_result, result);
    InterlockedExchange(&g_state.last_chat_error, static_cast<LONG>(gle));
    LogMessage(kLogError, "chat dispatch wait failed wait_rc=%lu gle=%lu mode=%d result=%ld", wait_rc, gle, mode, result);
    return FALSE;
  }

  if (out_rc != nullptr) {
    *out_rc = result;
  }
  if (out_error != nullptr) {
    *out_error = last_error;
  }

  return last_error == ERROR_SUCCESS;
}

BOOL RefreshCharacterIdentity(DWORD* out_error) {
  if (!EnsureHookInstalled()) {
    const DWORD gle = GetLastError();
    if (out_error != nullptr) {
      *out_error = gle;
    }
    InterlockedExchange(&g_state.identity_error, static_cast<LONG>(gle));
    return FALSE;
  }

  if (InterlockedCompareExchange(&g_state.pending_identity.busy, 1, 0) != 0) {
    if (out_error != nullptr) {
      *out_error = ERROR_BUSY;
    }
    InterlockedExchange(&g_state.identity_error, ERROR_BUSY);
    LogMessage(kLogDebug, "identity refresh rejected because a previous refresh is still in flight");
    return FALSE;
  }

  const LONG request_id = InterlockedIncrement(&g_state.pending_identity.sequence_seed);
  InterlockedExchange(&g_state.pending_identity.request_id, request_id);
  InterlockedExchange(&g_state.pending_identity.last_error, ERROR_IO_PENDING);
  ResetEvent(g_state.pending_identity.event);

  if (!PostMessageA(g_state.hwnd, kMsgRefreshIdentity, 0, static_cast<LPARAM>(request_id))) {
    const DWORD gle = GetLastError();
    InterlockedExchange(&g_state.pending_identity.busy, 0);
    InterlockedExchange(&g_state.identity_error, static_cast<LONG>(gle));
    if (out_error != nullptr) {
      *out_error = gle;
    }
    LogMessage(kLogError, "PostMessageA(identity refresh) failed gle=%lu", gle);
    return FALSE;
  }

  const DWORD wait_rc = WaitForSingleObject(g_state.pending_identity.event, kDispatchTimeoutMs);
  const DWORD last_error = static_cast<DWORD>(InterlockedCompareExchange(&g_state.pending_identity.last_error, 0, 0));
  InterlockedExchange(&g_state.pending_identity.busy, 0);

  if (wait_rc != WAIT_OBJECT_0) {
    const DWORD gle = (wait_rc == WAIT_TIMEOUT) ? WAIT_TIMEOUT : GetLastError();
    InterlockedExchange(&g_state.identity_error, static_cast<LONG>(gle));
    if (out_error != nullptr) {
      *out_error = gle;
    }
    LogMessage(kLogError, "identity refresh wait failed wait_rc=%lu gle=%lu", wait_rc, gle);
    return FALSE;
  }

  if (out_error != nullptr) {
    *out_error = last_error;
  }
  return last_error == ERROR_SUCCESS;
}

BOOL HandlePipeClient(HANDLE pipe) {
  for (;;) {
    PipeHeader header = {};
    if (!ReadExact(pipe, &header, sizeof(header))) {
      return FALSE;
    }

    if (header.size > kPipeBufferSize) {
      LogMessage(kLogError, "rejecting oversized pipe payload op=%u size=%u", header.op, header.size);
      return FALSE;
    }

    BYTE payload[kPipeBufferSize] = {};
    if (header.size > 0 && !ReadExact(pipe, payload, header.size)) {
      return FALSE;
    }

    switch (header.op) {
      case kOpQuery: {
        EnsureHookInstalled();
        if (InterlockedCompareExchange(&g_state.quickbar_this, 0, 0) == 0) {
          DiscoverQuickbarPanelByScan("query");
        }
        RefreshCharacterIdentity(nullptr);
        QueryResponse response = {};
        response.module_base = static_cast<uint32_t>(GetProcessImageBase());
        response.hook_wndproc = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(&SimKeysWndProc));
        response.hwnd = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(g_state.hwnd));
        response.current_wndproc = (g_state.hwnd != nullptr)
            ? static_cast<uint32_t>(GetWindowLongPtrA(g_state.hwnd, GWLP_WNDPROC))
            : 0;
        response.original_wndproc = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(g_state.original_wndproc));
        response.window_thread_id = g_state.window_thread_id;
        response.installed = static_cast<uint32_t>(InterlockedCompareExchange(&g_state.installed, 0, 0));
        response.expected_runtime_nwn_wndproc = RebaseAddress(kExpectedNwnWndProc);
        response.expected_runtime_key_pre_dispatch = RebaseAddress(kExpectedKeyPreDispatch);
        response.expected_runtime_dispatcher_thunk = RebaseAddress(kExpectedDispatcherThunk);
        response.expected_runtime_dispatcher_slot0 = RebaseAddress(kExpectedDispatcherSlot0);
        response.app_global_slot = static_cast<uint32_t>(kAppGlobalSlotAddress);
        response.app_holder = ReadAppHolderPointer();
        response.app_object = ReadAppObjectPointer();
        response.app_inner = response.app_object != 0 ? SafeReadPointer32(static_cast<uintptr_t>(response.app_object) + 4) : 0;
        response.dispatcher_ptr = response.app_inner != 0 ? SafeReadPointer32(static_cast<uintptr_t>(response.app_inner) + 0x24) : 0;
        response.gate_90 = response.app_inner != 0 ? SafeReadPointer32(static_cast<uintptr_t>(response.app_inner) + 0x90) : 0;
        response.gate_94 = response.app_inner != 0 ? SafeReadPointer32(static_cast<uintptr_t>(response.app_inner) + 0x94) : 0;
        response.gate_98 = response.app_inner != 0 ? SafeReadPointer32(static_cast<uintptr_t>(response.app_inner) + 0x98) : 0;
        response.quickbar_exec = RebaseAddress(kExpectedQuickbarExec);
        response.quickbar_slot_dispatch = RebaseAddress(kExpectedQuickbarSlotDispatch);
        response.quickbar_panel_vtable = RebaseAddress(kExpectedQuickbarVtable);
        response.quickbar_slot_ptr = static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_slot_ptr, 0, 0));
        response.quickbar_this = static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_this, 0, 0));
        response.quickbar_page = InterlockedCompareExchange(&g_state.quickbar_page, 0, 0);
        response.quickbar_slot = InterlockedCompareExchange(&g_state.quickbar_slot, 0, 0);
        response.quickbar_slot_type = InterlockedCompareExchange(&g_state.quickbar_slot_type, 0, 0);
        response.quickbar_calls = InterlockedCompareExchange(&g_state.quickbar_calls, 0, 0);
        response.quickbar_scan_attempts = InterlockedCompareExchange(&g_state.quickbar_scan_attempts, 0, 0);
        response.quickbar_scan_hits = InterlockedCompareExchange(&g_state.quickbar_scan_hits, 0, 0);
        response.last_vk = InterlockedCompareExchange(&g_state.last_vk, 0, 0);
        response.last_rc = InterlockedCompareExchange(&g_state.last_result, 0, 0);
        response.last_error = InterlockedCompareExchange(&g_state.last_error, 0, 0);
        response.log_level = InterlockedCompareExchange(&g_state.log_level, 0, 0);
        response.player_object = static_cast<uint32_t>(InterlockedCompareExchange(&g_state.player_object, 0, 0));
        response.identity_refresh_count = InterlockedCompareExchange(&g_state.identity_refresh_count, 0, 0);
        response.identity_error = InterlockedCompareExchange(&g_state.identity_error, 0, 0);
        response.quickbar_item_mask_low =
            static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_item_mask_low, 0, 0));
        response.quickbar_item_mask_high =
            static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_item_mask_high, 0, 0));
        response.quickbar_equipped_mask_low =
            static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_equipped_mask_low, 0, 0));
        response.quickbar_equipped_mask_high =
            static_cast<uint32_t>(InterlockedCompareExchange(&g_state.quickbar_equipped_mask_high, 0, 0));
        CopyStoredCharacterName(response.character_name, ARRAYSIZE(response.character_name));

        if (!WriteResponse(pipe, kOpQuery, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      case kOpSnapshotText: {
        if (InterlockedCompareExchange(&g_state.quickbar_this, 0, 0) == 0) {
          DiscoverQuickbarPanelByScan("snapshot");
        }
        RefreshCharacterIdentity(nullptr);
        char snapshot[4096] = {};
        BuildSnapshotText("pipe-query", snapshot, sizeof(snapshot));
        if (!WriteResponse(pipe, kOpSnapshotText, snapshot, static_cast<uint32_t>(strlen(snapshot)))) {
          return FALSE;
        }
        break;
      }

      case kOpChatSend: {
        ChatSendResponse response = {};
        if (header.size < sizeof(int32_t) * 2) {
          response.last_error = ERROR_INVALID_DATA;
          InterlockedExchange(&g_state.last_chat_result, 0);
          InterlockedExchange(&g_state.last_chat_error, ERROR_INVALID_DATA);
        } else {
          const int32_t mode = *reinterpret_cast<const int32_t*>(payload + 0);
          const int32_t text_length = *reinterpret_cast<const int32_t*>(payload + sizeof(int32_t));
          const DWORD expected_size = static_cast<DWORD>(sizeof(int32_t) * 2 + (text_length > 0 ? text_length : 0));
          if (text_length < 0 || header.size != expected_size || text_length >= kPendingChatCapacity) {
            response.last_error = ERROR_INVALID_DATA;
            InterlockedExchange(&g_state.last_chat_mode, mode);
            InterlockedExchange(&g_state.last_chat_result, 0);
            InterlockedExchange(&g_state.last_chat_error, ERROR_INVALID_DATA);
          } else {
            char text[kPendingChatCapacity] = {};
            if (text_length > 0) {
              memcpy(text, payload + sizeof(int32_t) * 2, static_cast<size_t>(text_length));
            }
            text[text_length] = '\0';

            LONG rc = 0;
            DWORD last_error = ERROR_SUCCESS;
            response.success = TriggerChatMessage(text, mode, &rc, &last_error) ? 1 : 0;
            response.mode = mode;
            response.rc = rc;
            response.last_error = static_cast<int32_t>(last_error);
            LogMessage(kLogInfo, "chat request mode=%ld success=%ld rc=%ld err=%ld text=%s", mode, response.success, response.rc, response.last_error, text);
          }
        }

        if (!WriteResponse(pipe, kOpChatSend, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      case kOpChatPoll: {
        ChatPollRequest request = {};
        if (header.size == sizeof(ChatPollRequest)) {
          memcpy(&request, payload, sizeof(request));
        } else {
          request.after_sequence = 0;
          request.max_lines = 20;
        }

        BYTE response[kPipeBufferSize] = {};
        DWORD response_size = 0;
        if (!BuildChatPollResponse(request, response, sizeof(response), &response_size)) {
          return FALSE;
        }
        if (!WriteResponse(pipe, kOpChatPoll, response, response_size)) {
          return FALSE;
        }
        break;
      }

      case kOpTriggerPageSlot: {
        TriggerResponse response = {};
        if (header.size != sizeof(int32_t) * 2) {
          response.last_error = ERROR_INVALID_DATA;
          UpdateLastOperation(0, 0, ERROR_INVALID_DATA);
        } else {
          const int32_t slot = *reinterpret_cast<const int32_t*>(payload + 0);
          const int32_t page = *reinterpret_cast<const int32_t*>(payload + sizeof(int32_t));
          const UINT vk = SlotToVirtualKey(slot);
          LONG rc = 0;
          DWORD last_error = ERROR_SUCCESS;
          response.success = TriggerQuickbarPageSlot(page, slot, &rc, &last_error) ? 1 : 0;
          response.vk = static_cast<int32_t>(vk);
          response.rc = rc;
          response.aux_rc = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
          response.last_error = static_cast<int32_t>(last_error);
          response.path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
          LogMessage(
              kLogInfo,
              "page-slot request page=%ld slot=%ld vk=0x%02X success=%ld rc=%ld aux=%ld path=%ld err=%ld",
              page,
              slot,
              vk,
              response.success,
              response.rc,
              response.aux_rc,
              response.path,
              response.last_error);
        }

        if (!WriteResponse(pipe, kOpTriggerPageSlot, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      case kOpTriggerSlot: {
        TriggerResponse response = {};
        if (header.size != sizeof(int32_t)) {
          response.last_error = ERROR_INVALID_DATA;
          UpdateLastOperation(0, 0, ERROR_INVALID_DATA);
        } else {
          const int32_t slot = *reinterpret_cast<int32_t*>(payload);
          const UINT vk = SlotToVirtualKey(slot);
          LONG rc = 0;
          DWORD last_error = ERROR_SUCCESS;
          response.success = TriggerVirtualKey(vk, &rc, &last_error) ? 1 : 0;
          response.vk = static_cast<int32_t>(vk);
          response.rc = rc;
          response.aux_rc = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
          response.last_error = static_cast<int32_t>(last_error);
          response.path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
          LogMessage(kLogInfo, "slot request slot=%ld vk=0x%02X success=%ld rc=%ld aux=%ld path=%ld err=%ld", slot, vk, response.success, response.rc, response.aux_rc, response.path, response.last_error);
        }

        if (!WriteResponse(pipe, kOpTriggerSlot, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      case kOpTriggerVk: {
        TriggerResponse response = {};
        if (header.size != sizeof(int32_t)) {
          response.last_error = ERROR_INVALID_DATA;
          UpdateLastOperation(0, 0, ERROR_INVALID_DATA);
        } else {
          const UINT vk = static_cast<UINT>(*reinterpret_cast<int32_t*>(payload));
          LONG rc = 0;
          DWORD last_error = ERROR_SUCCESS;
          response.success = TriggerVirtualKey(vk, &rc, &last_error) ? 1 : 0;
          response.vk = static_cast<int32_t>(vk);
          response.rc = rc;
          response.aux_rc = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
          response.last_error = static_cast<int32_t>(last_error);
          response.path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
          LogMessage(kLogInfo, "vk request vk=0x%02X success=%ld rc=%ld aux=%ld path=%ld err=%ld", vk, response.success, response.rc, response.aux_rc, response.path, response.last_error);
        }

        if (!WriteResponse(pipe, kOpTriggerVk, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      case kOpSetLog: {
        int32_t new_level = kLogInfo;
        if (header.size == sizeof(int32_t)) {
          new_level = *reinterpret_cast<int32_t*>(payload);
        }
        if (new_level < kLogError) {
          new_level = kLogError;
        }
        if (new_level > kLogDebug) {
          new_level = kLogDebug;
        }
        InterlockedExchange(&g_state.log_level, new_level);
        if (!WriteResponse(pipe, kOpSetLog, &new_level, sizeof(new_level))) {
          return FALSE;
        }
        break;
      }

      case kOpReplayLast: {
        TriggerResponse response = {};
        const UINT vk = static_cast<UINT>(InterlockedCompareExchange(&g_state.last_vk, 0, 0));
        LONG rc = 0;
        DWORD last_error = ERROR_SUCCESS;
        response.success = TriggerVirtualKey(vk, &rc, &last_error) ? 1 : 0;
        response.vk = static_cast<int32_t>(vk);
        response.rc = rc;
        response.aux_rc = InterlockedCompareExchange(&g_state.pending.aux_result, 0, 0);
        response.last_error = static_cast<int32_t>(last_error);
        response.path = InterlockedCompareExchange(&g_state.pending.dispatch_path, 0, 0);
        if (!WriteResponse(pipe, kOpReplayLast, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }

      default: {
        TriggerResponse response = {};
        response.last_error = ERROR_INVALID_FUNCTION;
        UpdateLastOperation(0, 0, ERROR_INVALID_FUNCTION);
        if (!WriteResponse(pipe, header.op, &response, sizeof(response))) {
          return FALSE;
        }
        break;
      }
    }
  }
}

DWORD WINAPI PipeThreadMain(LPVOID) {
  char pipe_name[64] = {};
  _snprintf_s(pipe_name, sizeof(pipe_name), _TRUNCATE, "\\\\.\\pipe\\simkeys_%lu", GetCurrentProcessId());

  LogMessage(kLogInfo, "pipe thread starting on %s", pipe_name);

  for (;;) {
    HANDLE pipe = CreateNamedPipeA(
        pipe_name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1,
        kPipeBufferSize,
        kPipeBufferSize,
        0,
        nullptr);

    if (pipe == INVALID_HANDLE_VALUE) {
      const DWORD gle = GetLastError();
      InterlockedExchange(&g_state.pipe_state, -1);
      InterlockedExchange(&g_state.pipe_thread_error, static_cast<LONG>(gle));
      if (g_state.pipe_ready_event != nullptr) {
        SetEvent(g_state.pipe_ready_event);
      }
      UpdateLastOperation(0, 0, gle);
      LogMessage(kLogError, "CreateNamedPipeA failed gle=%lu", gle);
      return gle == ERROR_SUCCESS ? 1 : gle;
    }

    if (InterlockedCompareExchange(&g_state.pipe_state, 1, 0) == 0) {
      InterlockedExchange(&g_state.pipe_thread_error, ERROR_SUCCESS);
      if (g_state.pipe_ready_event != nullptr) {
        SetEvent(g_state.pipe_ready_event);
      }
      LogMessage(kLogInfo, "pipe server is ready on %s", pipe_name);
    }

    BOOL connected = ConnectNamedPipe(pipe, nullptr);
    if (!connected) {
      const DWORD gle = GetLastError();
      if (gle != ERROR_PIPE_CONNECTED) {
        CloseHandle(pipe);
        LogMessage(kLogError, "ConnectNamedPipe failed gle=%lu", gle);
        Sleep(250);
        continue;
      }
    }

    LogMessage(kLogDebug, "pipe client connected");
    HandlePipeClient(pipe);
    FlushFileBuffers(pipe);
    DisconnectNamedPipe(pipe);
    CloseHandle(pipe);
    LogMessage(kLogDebug, "pipe client disconnected");
  }
}

}  // namespace

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_state.module = module;
    DisableThreadLibraryCalls(module);
  }
  return TRUE;
}

SIMKEYS_API DWORD WINAPI InitSimKeys(LPVOID) {
  const LONG previous = InterlockedCompareExchange(&g_state.initialized, 1, 0);
  if (previous != 0) {
    const LONG pipe_state = InterlockedCompareExchange(&g_state.pipe_state, 0, 0);
    if (pipe_state == 1) {
      return 2;
    }
    return ERROR_PIPE_NOT_CONNECTED;
  }

  InitializeCriticalSection(&g_state.lock);
  g_state.lock_ready = true;
  InitializeCriticalSection(&g_state.chat_lock);
  g_state.chat_lock_ready = true;
  InitializeCriticalSection(&g_state.log_lock);
  g_state.log_lock_ready = true;
  g_state.log_file = nullptr;
  g_state.module_path[0] = '\0';
  g_state.log_path[0] = '\0';
  g_state.character_name[0] = '\0';
  ZeroMemory(g_state.chat_lines, sizeof(g_state.chat_lines));
  InterlockedExchange(&g_state.pipe_state, 0);
  InterlockedExchange(&g_state.pipe_thread_error, ERROR_IO_PENDING);
  EnsureLogFileReady();
  g_state.pending.event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
  if (g_state.pending.event == nullptr) {
    if (g_state.log_file != nullptr) {
      CloseHandle(g_state.log_file);
      g_state.log_file = nullptr;
    }
    g_state.log_lock_ready = false;
    DeleteCriticalSection(&g_state.log_lock);
    g_state.chat_lock_ready = false;
    DeleteCriticalSection(&g_state.chat_lock);
    g_state.lock_ready = false;
    DeleteCriticalSection(&g_state.lock);
    g_state.initialized = 0;
    return 0;
  }

  g_state.pending_chat.event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
  if (g_state.pending_chat.event == nullptr) {
    CloseHandle(g_state.pending.event);
    g_state.pending.event = nullptr;
    if (g_state.log_file != nullptr) {
      CloseHandle(g_state.log_file);
      g_state.log_file = nullptr;
    }
    g_state.log_lock_ready = false;
    DeleteCriticalSection(&g_state.log_lock);
    g_state.chat_lock_ready = false;
    DeleteCriticalSection(&g_state.chat_lock);
    g_state.lock_ready = false;
    DeleteCriticalSection(&g_state.lock);
    g_state.initialized = 0;
    return 0;
  }

  g_state.pending_identity.event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
  if (g_state.pending_identity.event == nullptr) {
    CloseHandle(g_state.pending_chat.event);
    g_state.pending_chat.event = nullptr;
    CloseHandle(g_state.pending.event);
    g_state.pending.event = nullptr;
    if (g_state.log_file != nullptr) {
      CloseHandle(g_state.log_file);
      g_state.log_file = nullptr;
    }
    g_state.log_lock_ready = false;
    DeleteCriticalSection(&g_state.log_lock);
    g_state.chat_lock_ready = false;
    DeleteCriticalSection(&g_state.chat_lock);
    g_state.lock_ready = false;
    DeleteCriticalSection(&g_state.lock);
    g_state.initialized = 0;
    return 0;
  }

  g_state.pipe_ready_event = CreateEventA(nullptr, TRUE, FALSE, nullptr);
  if (g_state.pipe_ready_event == nullptr) {
    CloseHandle(g_state.pending_identity.event);
    g_state.pending_identity.event = nullptr;
    CloseHandle(g_state.pending_chat.event);
    g_state.pending_chat.event = nullptr;
    CloseHandle(g_state.pending.event);
    g_state.pending.event = nullptr;
    if (g_state.log_file != nullptr) {
      CloseHandle(g_state.log_file);
      g_state.log_file = nullptr;
    }
    g_state.log_lock_ready = false;
    DeleteCriticalSection(&g_state.log_lock);
    g_state.chat_lock_ready = false;
    DeleteCriticalSection(&g_state.chat_lock);
    g_state.lock_ready = false;
    DeleteCriticalSection(&g_state.lock);
    g_state.initialized = 0;
    return 0;
  }

  InterlockedExchange(&g_state.log_level, kLogInfo);
  g_state.pipe_thread = CreateThread(nullptr, 0, PipeThreadMain, nullptr, 0, nullptr);
  if (g_state.pipe_thread == nullptr) {
    CloseHandle(g_state.pipe_ready_event);
    g_state.pipe_ready_event = nullptr;
    CloseHandle(g_state.pending_identity.event);
    g_state.pending_identity.event = nullptr;
    CloseHandle(g_state.pending_chat.event);
    g_state.pending_chat.event = nullptr;
    CloseHandle(g_state.pending.event);
    g_state.pending.event = nullptr;
    if (g_state.log_file != nullptr) {
      CloseHandle(g_state.log_file);
      g_state.log_file = nullptr;
    }
    g_state.log_lock_ready = false;
    DeleteCriticalSection(&g_state.log_lock);
    g_state.chat_lock_ready = false;
    DeleteCriticalSection(&g_state.chat_lock);
    g_state.lock_ready = false;
    DeleteCriticalSection(&g_state.lock);
    g_state.initialized = 0;
    return 0;
  }

  const DWORD pipe_wait = WaitForSingleObject(g_state.pipe_ready_event, kPipeStartupTimeoutMs);
  const LONG pipe_state = InterlockedCompareExchange(&g_state.pipe_state, 0, 0);
  const DWORD pipe_error = static_cast<DWORD>(InterlockedCompareExchange(&g_state.pipe_thread_error, 0, 0));
  if (pipe_wait != WAIT_OBJECT_0 || pipe_state != 1) {
    const DWORD failure = pipe_error != ERROR_IO_PENDING && pipe_error != ERROR_SUCCESS
        ? pipe_error
        : (pipe_wait == WAIT_TIMEOUT ? WAIT_TIMEOUT : ERROR_PIPE_NOT_CONNECTED);
    UpdateLastOperation(0, 0, failure);
    LogMessage(kLogError, "pipe startup failed wait=%lu pipeState=%ld pipeErr=%lu", pipe_wait, pipe_state, pipe_error);
    return failure;
  }

  EnsureHookInstalled();
  InstallQuickbarTraceHook();
  InstallQuickbarSlotTraceHook();
  InstallChatWindowLogHook();
  DiscoverQuickbarPanelByScan("init");
  LogMessage(kLogInfo, "InitSimKeys complete");
  return 1;
}
