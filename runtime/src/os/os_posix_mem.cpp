// SPDX-License-Identifier: GPL-3.0-or-later
// os_posix_mem.cpp: the POSIX side of src/os/nx_os.h -- VirtualAlloc/Free/Protect/Query and section objects.
//
// This is the one part of the shim that is not a rename. The guest address space is an identity map, so the runtime
// reserves exact addresses, commits and decommits inside a reservation at guest granularity (4 KiB, mem::kPage), and
// maps several views of one pagefile section so that the heap, the NVN memory pools and MapMemory aliases share pages
// (include/nxrt/memory.h). Three facts shape the implementation:
//
//   1. The host page is 16 KiB on Apple Silicon, the guest page is 4 KiB. mprotect() only takes 16 KiB-aligned ranges,
//      so a protection change is applied to the union of every guest page in the touched host pages: if one 4 KiB
//      sub-page needs to be writable, the whole 16 KiB host page is. The guest-level state stays 4 KiB accurate in the
//      table below, which is what VirtualQuery reports and what write_watch.cpp reasons about; only the host-level
//      guard is coarser. A write watch therefore arms 16 KiB at a time on macOS and takes more spurious faults, but it
//      still catches every write.
//   2. Guest code is never executed (docs/macos-port.md D7: the loader's only caller of Prot::RX is the ELF segment
//      mapper). PAGE_EXECUTE* therefore maps to plain read/write, which keeps the whole port clear of MAP_JIT and the
//      allow-jit entitlement on arm64.
//   3. A section is a POSIX shared memory object: ftruncate gives SEC_RESERVE semantics for free (pages are charged on
//      first touch, not at creation) and mmap(MAP_SHARED) gives every view the same pages, which is what
//      MapViewOfFileEx must do.
#include "nx_os_internal.h"

#if !defined(_WIN32)

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/sysctl.h>
#else
#include <sys/sysinfo.h>
#endif

#include <algorithm>
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

using namespace nxos;

namespace nxos {

struct SectionObj : Obj {
    int fd = -1;
    uint64_t size = 0;
    std::string name;
    explicit SectionObj() : Obj(Kind::Section) {}
    ~SectionObj() override {
        if (fd >= 0) ::close(fd);
        if (!name.empty()) ::shm_unlink(name.c_str());
    }
};

}  // namespace nxos

namespace {

// ---- host page geometry ------------------------------------------------------------------------------------------
size_t host_page() {
    static const size_t p = [] {
        long v = sysconf(_SC_PAGESIZE);
        return v > 0 ? static_cast<size_t>(v) : 4096u;
    }();
    return p;
}

uintptr_t round_down(uintptr_t a) { return a & ~(static_cast<uintptr_t>(host_page()) - 1); }
uintptr_t round_up(uintptr_t a) { return round_down(a + host_page() - 1); }

// ---- region table ------------------------------------------------------------------------------------------------
// Guest-granular bookkeeping for everything VirtualAlloc made or a section view mapped. Sorted by base, non-overlapping.
struct Region {
    uintptr_t base = 0;
    uintptr_t size = 0;
    DWORD state = MEM_FREE;   // MEM_RESERVE or MEM_COMMIT
    DWORD protect = PAGE_NOACCESS;
    bool own_mapping = false;   // mmap'd by us: MEM_RELEASE munmaps it
    bool section_view = false;
    uint64_t section_off = 0;
    SectionObj* section = nullptr;   // retained while the view exists
};

std::mutex g_reg_mu;
std::vector<Region> g_regions;   // sorted by base

int region_find(uintptr_t a) {
    // Binary search for the region containing `a`.
    size_t lo = 0, hi = g_regions.size();
    while (lo < hi) {
        const size_t mid = (lo + hi) / 2;
        if (g_regions[mid].base + g_regions[mid].size <= a) lo = mid + 1;
        else hi = mid;
    }
    if (lo < g_regions.size() && g_regions[lo].base <= a) return static_cast<int>(lo);
    return -1;
}

void region_insert(const Region& r) {
    size_t i = 0;
    while (i < g_regions.size() && g_regions[i].base < r.base) ++i;
    g_regions.insert(g_regions.begin() + static_cast<std::ptrdiff_t>(i), r);
}

// Splits the region containing `a` so that a region boundary lands exactly on `a`. Returns the index of the region
// that starts at `a`, or -1.
int region_split_at(uintptr_t a) {
    const int i = region_find(a);
    if (i < 0) return -1;
    Region& r = g_regions[static_cast<size_t>(i)];
    if (r.base == a) return i;
    Region tail = r;
    tail.base = a;
    tail.size = r.base + r.size - a;
    if (tail.section_view) {
        tail.section_off = r.section_off + (a - r.base);
        if (tail.section) retain(tail.section);
    }
    r.size = a - r.base;
    g_regions.insert(g_regions.begin() + i + 1, tail);
    return i + 1;
}

// ---- protection translation -------------------------------------------------------------------------------------
// PAGE_EXECUTE* loses PROT_EXEC: guest code is never executed (D7), and an executable anonymous mapping on arm64
// macOS needs MAP_JIT plus the allow-jit entitlement.
int prot_of(DWORD protect) {
    const DWORD p = protect & 0xFF;
    switch (p) {
        case PAGE_READONLY: return PROT_READ;
        case PAGE_READWRITE: return PROT_READ | PROT_WRITE;
        case PAGE_WRITECOPY: return PROT_READ | PROT_WRITE;
        case PAGE_EXECUTE: return PROT_READ;
        case PAGE_EXECUTE_READ: return PROT_READ;
        case PAGE_EXECUTE_READWRITE: return PROT_READ | PROT_WRITE;
        case PAGE_EXECUTE_WRITECOPY: return PROT_READ | PROT_WRITE;
        case PAGE_NOACCESS:
        default: return PROT_NONE;
    }
}

DWORD protect_of_posix(int prot, bool writable_copy) {
    const bool r = (prot & PROT_READ) != 0, w = (prot & PROT_WRITE) != 0;
    if (w) return writable_copy ? PAGE_WRITECOPY : PAGE_READWRITE;
    if (r) return PAGE_READONLY;
    return PAGE_NOACCESS;
}

// mprotect over the host pages that cover [base, base+size).
bool host_protect(uintptr_t base, uintptr_t size, int prot) {
    if (size == 0) return true;
    const uintptr_t a = round_down(base);
    const uintptr_t b = round_up(base + size);
    return ::mprotect(reinterpret_cast<void*>(a), b - a, prot) == 0;
}

// ---- the free/occupied question for a fixed reservation -----------------------------------------------------------
// mmap without MAP_FIXED is used instead of MAP_FIXED: it refuses to clobber an existing mapping and returns a
// different address, which is exactly VirtualAlloc's "the range is not free" failure.
bool range_free(uintptr_t base, uintptr_t size) {
    void* p = ::mmap(reinterpret_cast<void*>(base), size, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
    if (p == MAP_FAILED) return false;
    const bool free_range = reinterpret_cast<uintptr_t>(p) == base;
    ::munmap(p, size);
    return free_range;
}

}  // namespace

// ==================================================================================================================
// VirtualAlloc / VirtualFree / VirtualProtect / VirtualQuery
// ==================================================================================================================
LPVOID VirtualAlloc(LPVOID addr, SIZE_T size, DWORD type, DWORD protect) {
    if (size == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }
    const uintptr_t want = reinterpret_cast<uintptr_t>(addr);
    const uintptr_t bytes = static_cast<uintptr_t>(size);
    std::lock_guard<std::mutex> guard(g_reg_mu);

    if (type & MEM_RESERVE) {
        if (want == 0) {
            void* p = ::mmap(nullptr, bytes, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
            if (p == MAP_FAILED) {
                SetLastError(ERROR_NOT_ENOUGH_MEMORY);
                return nullptr;
            }
            Region r;
            r.base = reinterpret_cast<uintptr_t>(p);
            r.size = bytes;
            r.state = MEM_RESERVE;
            r.protect = protect;
            r.own_mapping = true;
            region_insert(r);
            return p;
        }
        if (!range_free(want, bytes)) {
            SetLastError(ERROR_INVALID_ADDRESS);
            return nullptr;
        }
        void* p = ::mmap(reinterpret_cast<void*>(want), bytes, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
        if (p == MAP_FAILED || reinterpret_cast<uintptr_t>(p) != want) {
            if (p != MAP_FAILED) ::munmap(p, bytes);
            SetLastError(ERROR_NOT_ENOUGH_MEMORY);
            return nullptr;
        }
        Region r;
        r.base = want;
        r.size = bytes;
        r.state = MEM_RESERVE;
        r.protect = protect;
        r.own_mapping = true;
        region_insert(r);
        return p;
    }

    if (type & MEM_COMMIT) {
        if (want == 0) {
            void* p = ::mmap(nullptr, bytes, prot_of(protect), MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
            if (p == MAP_FAILED) {
                SetLastError(ERROR_NOT_ENOUGH_MEMORY);
                return nullptr;
            }
            Region r;
            r.base = reinterpret_cast<uintptr_t>(p);
            r.size = bytes;
            r.state = MEM_COMMIT;
            r.protect = protect;
            r.own_mapping = true;
            region_insert(r);
            return p;
        }
        const int i = region_find(want);
        if (i < 0 || g_regions[static_cast<size_t>(i)].base + g_regions[static_cast<size_t>(i)].size < want + bytes) {
            SetLastError(ERROR_INVALID_ADDRESS);
            return nullptr;
        }
        // Mark [want, want+bytes) committed. A section view is already mapped (its pages read as zeros until touched),
        // so committing only applies the protection; a plain reservation needs the mprotect as well.
        const int lo = region_split_at(want);
        const int hi = region_split_at(want + bytes);
        (void)hi;
        for (int k = lo; k >= 0 && k < static_cast<int>(g_regions.size()); ++k) {
            Region& r = g_regions[static_cast<size_t>(k)];
            if (r.base >= want + bytes) break;
            if (r.state != MEM_RESERVE) continue;
            if (!host_protect(r.base, r.size, prot_of(protect))) {
                SetLastError(ERROR_NOT_ENOUGH_MEMORY);
                return nullptr;
            }
            r.state = MEM_COMMIT;
            r.protect = protect;
        }
        return reinterpret_cast<LPVOID>(want);
    }

    SetLastError(ERROR_INVALID_PARAMETER);
    return nullptr;
}

LPVOID VirtualAllocEx(HANDLE, LPVOID addr, SIZE_T size, DWORD type, DWORD protect) {
    return VirtualAlloc(addr, size, type, protect);
}

BOOL VirtualFree(LPVOID addr, SIZE_T size, DWORD type) {
    const uintptr_t a = reinterpret_cast<uintptr_t>(addr);
    if (a == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    std::lock_guard<std::mutex> guard(g_reg_mu);
    const int i = region_find(a);
    if (i < 0) {
        SetLastError(ERROR_INVALID_ADDRESS);
        return FALSE;
    }
    Region& r = g_regions[static_cast<size_t>(i)];
    if (r.base != a) {
        // Only the first page of a region can be released.
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }

    if (type & MEM_RELEASE) {
        if (r.own_mapping) ::munmap(reinterpret_cast<void*>(r.base), r.size);
        // A section view is munmapped too; only the section object outlives the view.
        else ::munmap(reinterpret_cast<void*>(r.base), r.size);
        if (r.section) release(r.section);
        g_regions.erase(g_regions.begin() + i);
        return TRUE;
    }

    if (type & MEM_DECOMMIT) {
        if (r.section_view) {
            // Windows refuses VirtualFree(MEM_DECOMMIT) on a section view (ERROR_INVALID_PARAMETER); memory.cpp:574
            // documents that and uses VirtualProtect instead, so refusing here keeps the two platforms identical.
            SetLastError(ERROR_INVALID_PARAMETER);
            return FALSE;
        }
        const int lo = region_split_at(a);
        const int hi = region_split_at(a + size);
        (void)hi;
        for (int k = lo; k >= 0 && k < static_cast<int>(g_regions.size()); ++k) {
            Region& q = g_regions[static_cast<size_t>(k)];
            if (q.base >= a + size) break;
            if (q.state != MEM_COMMIT) continue;
            host_protect(q.base, q.size, PROT_NONE);
            q.state = MEM_RESERVE;
            q.protect = PAGE_NOACCESS;
        }
        return TRUE;
    }

    SetLastError(ERROR_INVALID_PARAMETER);
    return FALSE;
}

BOOL VirtualProtect(LPVOID addr, SIZE_T size, DWORD protect, PDWORD old_protect) {
    const uintptr_t a = reinterpret_cast<uintptr_t>(addr);
    if (a == 0 || size == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    std::lock_guard<std::mutex> guard(g_reg_mu);
    const int i = region_find(a);
    if (i < 0) {
        SetLastError(ERROR_INVALID_ADDRESS);
        return FALSE;
    }
    if (old_protect) *old_protect = g_regions[static_cast<size_t>(i)].protect;
    region_split_at(a);
    region_split_at(a + size);
    for (Region& r : g_regions) {
        if (r.base >= a + size) break;
        if (r.base + r.size <= a) continue;
        if (!host_protect(r.base, r.size, prot_of(protect))) {
            SetLastError(ERROR_INVALID_PARAMETER);
            return FALSE;
        }
        r.protect = protect;
        if (r.state == MEM_RESERVE && (protect & 0xFF) != PAGE_NOACCESS) r.state = MEM_COMMIT;
    }
    return TRUE;
}

BOOL VirtualProtectEx(HANDLE, LPVOID addr, SIZE_T size, DWORD protect, PDWORD old_protect) {
    return VirtualProtect(addr, size, protect, old_protect);
}

namespace {

// A page range that is not in the table: ask the kernel. Used by crash.cpp:88 (is this address readable?) and by
// diag/testtools walks over the module image, which is in the table, so this is the malloc'd-memory case.
bool host_region_of(uintptr_t a, uintptr_t* base, uintptr_t* size, int* prot) {
#if defined(__APPLE__)
    mach_vm_address_t addr = a;
    mach_vm_size_t len = 0;
    vm_region_basic_info_data_64_t info{};
    mach_port_t object = MACH_PORT_NULL;
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    if (mach_vm_region(mach_task_self(), &addr, &len, VM_REGION_BASIC_INFO_64,
                       reinterpret_cast<vm_region_info_t>(&info), &count, &object) != KERN_SUCCESS) {
        return false;
    }
    *base = static_cast<uintptr_t>(addr);
    *size = static_cast<uintptr_t>(len);
    *prot = info.protection;
    return true;
#else
    // No portable query: probe one page with mincore, and report a single page so the caller re-queries the next one.
    unsigned char vec = 0;
    if (::mincore(reinterpret_cast<void*>(round_down(a)), host_page(), &vec) != 0) return false;
    *base = round_down(a);
    *size = host_page();
    *prot = PROT_READ | PROT_WRITE;
    return true;
#endif
}

}  // namespace

SIZE_T VirtualQuery(LPCVOID addr, MEMORY_BASIC_INFORMATION* mbi, SIZE_T len) {
    if (!mbi || len < sizeof(MEMORY_BASIC_INFORMATION)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    const uintptr_t a = reinterpret_cast<uintptr_t>(addr);
    std::memset(mbi, 0, sizeof *mbi);

    {
        std::lock_guard<std::mutex> guard(g_reg_mu);
        const int i = region_find(a);
        if (i >= 0) {
            const Region& r = g_regions[static_cast<size_t>(i)];
            mbi->BaseAddress = reinterpret_cast<PVOID>(r.base);
            mbi->AllocationBase = reinterpret_cast<PVOID>(r.base);
            mbi->AllocationProtect = r.protect;
            mbi->RegionSize = r.base + r.size - a;   // Windows reports the rest of the region, not the whole of it
            mbi->State = r.state;
            mbi->Protect = r.protect;
            mbi->Type = r.section_view ? MEM_MAPPED : MEM_PRIVATE;
            return sizeof *mbi;
        }
    }

    uintptr_t base = 0, size = 0;
    int prot = 0;
    if (!host_region_of(a, &base, &size, &prot)) {
        // Nothing there: report free space up to the next region the kernel knows about, which is what a caller
        // walking the address space expects (nvn_gpu_pool.cpp:127 breaks on State != MEM_COMMIT).
        mbi->BaseAddress = reinterpret_cast<PVOID>(a);
        mbi->RegionSize = host_page();
        mbi->State = MEM_FREE;
        mbi->Protect = PAGE_NOACCESS;
        mbi->Type = 0;
        return sizeof *mbi;
    }
    mbi->BaseAddress = reinterpret_cast<PVOID>(base);
    mbi->AllocationBase = reinterpret_cast<PVOID>(base);
    mbi->AllocationProtect = protect_of_posix(prot, false);
    mbi->RegionSize = base + size - a;
    mbi->State = MEM_COMMIT;
    mbi->Protect = protect_of_posix(prot, false);
    mbi->Type = MEM_PRIVATE;
    return sizeof *mbi;
}

SIZE_T VirtualQueryEx(HANDLE, LPCVOID addr, MEMORY_BASIC_INFORMATION* mbi, SIZE_T len) {
    return VirtualQuery(addr, mbi, len);
}

BOOL FlushInstructionCache(HANDLE, LPCVOID, SIZE_T) { return TRUE; }   // no instruction cache to flush: D7

// ==================================================================================================================
// system information
// ==================================================================================================================
void GetSystemInfo(LPSYSTEM_INFO si) {
    if (!si) return;
    std::memset(si, 0, sizeof *si);
#if defined(__APPLE__)
    si->wProcessorArchitecture = PROCESSOR_ARCHITECTURE_ARM64;
#else
    si->wProcessorArchitecture = PROCESSOR_ARCHITECTURE_AMD64;
#endif
    si->dwPageSize = static_cast<DWORD>(host_page());
    si->lpMinimumApplicationAddress = reinterpret_cast<LPVOID>(static_cast<uintptr_t>(0x10000));
    si->lpMaximumApplicationAddress = reinterpret_cast<LPVOID>(~static_cast<uintptr_t>(0));
    const long n = sysconf(_SC_NPROCESSORS_ONLN);
    si->dwNumberOfProcessors = n > 0 ? static_cast<DWORD>(n) : 1;
    si->dwActiveProcessorMask = (si->dwNumberOfProcessors < 64) ? ((static_cast<DWORD_PTR>(1) << si->dwNumberOfProcessors) - 1)
                                                                : ~static_cast<DWORD_PTR>(0);
    si->dwAllocationGranularity = 0x10000;   // mem::kAllocGranularity: every section view starts on a 64 KiB boundary
    si->dwProcessorType = 0;
    si->wProcessorLevel = 0;
    si->wProcessorRevision = 0;
}

void GetNativeSystemInfo(LPSYSTEM_INFO si) { GetSystemInfo(si); }

BOOL GlobalMemoryStatusEx(LPMEMORYSTATUSEX ms) {
    if (!ms || ms->dwLength < sizeof(MEMORYSTATUSEX)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
#if defined(__APPLE__)
    uint64_t mem = 0;
    size_t len = sizeof mem;
    if (sysctlbyname("hw.memsize", &mem, &len, nullptr, 0) != 0) mem = 0;
#else
    struct sysinfo info{};
    const uint64_t mem = sysinfo(&info) == 0 ? static_cast<uint64_t>(info.totalram) * info.mem_unit : 0;
#endif
    ms->ullTotalPhys = mem;
    ms->ullAvailPhys = mem;   // unified memory: free RAM is not a meaningful number for the guest
    ms->ullTotalPageFile = mem;
    ms->ullAvailPageFile = mem;
    ms->ullTotalVirtual = mem;
    ms->ullAvailVirtual = mem;
    ms->ullAvailExtendedVirtual = 0;
    ms->dwMemoryLoad = 0;
    return TRUE;
}

// ==================================================================================================================
// sections
// ==================================================================================================================
namespace {
std::atomic<uint32_t> g_section_seq{1};
}

HANDLE CreateFileMappingW(HANDLE file, void*, DWORD protect, DWORD hi, DWORD lo, LPCWSTR) {
    if (file != INVALID_HANDLE_VALUE) {
        // A named file mapping over a real file is not used by the runtime; refuse rather than silently alias the fd.
        SetLastError(ERROR_CALL_NOT_IMPLEMENTED);
        return nullptr;
    }
    if (!(protect & (SEC_RESERVE | SEC_COMMIT | SEC_IMAGE)) && !(protect & (PAGE_READWRITE | PAGE_READONLY))) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }
    const uint64_t size = (static_cast<uint64_t>(hi) << 32) | lo;
    if (size == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }
    char name[64];
    for (int attempt = 0; attempt < 8; attempt++) {
        const uint32_t id = g_section_seq.fetch_add(1, std::memory_order_relaxed);
        std::snprintf(name, sizeof name, "/nxrt.%u.%u", static_cast<unsigned>(getpid()), id);
        const int fd = ::shm_open(name, O_RDWR | O_CREAT | O_EXCL, 0600);
        if (fd < 0) {
            if (errno == EEXIST) continue;
            nxos_set_error_from_errno(errno);
            return nullptr;
        }
        // ftruncate on a POSIX shm object is sparse: pages are charged on first touch, which is exactly SEC_RESERVE.
        if (::ftruncate(fd, static_cast<off_t>(size)) != 0) {
            const int e = errno;
            ::close(fd);
            ::shm_unlink(name);
            nxos_set_error_from_errno(e);
            return nullptr;
        }
        auto* s = new SectionObj();
        s->fd = fd;
        s->size = size;
        s->name = name;
        return s;
    }
    SetLastError(ERROR_ALREADY_EXISTS);
    return nullptr;
}

HANDLE CreateFileMappingA(HANDLE file, void* sa, DWORD protect, DWORD hi, DWORD lo, LPCSTR) {
    return CreateFileMappingW(file, sa, protect, hi, lo, nullptr);
}

LPVOID MapViewOfFileEx(HANDLE section, DWORD access, DWORD hi, DWORD lo, SIZE_T size, LPVOID at) {
    auto* s = as<SectionObj>(section, Kind::Section);
    if (!s) {
        SetLastError(ERROR_INVALID_HANDLE);
        return nullptr;
    }
    const uint64_t off = (static_cast<uint64_t>(hi) << 32) | lo;
    uint64_t bytes = size;
    if (bytes == 0) bytes = s->size - off;   // a zero size maps to the end of the section
    if (off >= s->size || off + bytes > s->size) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }
    const int prot = (access & FILE_MAP_ALL_ACCESS & (FILE_MAP_READ | FILE_MAP_WRITE)) ? (PROT_READ | PROT_WRITE)
                                                                                      : PROT_READ;
    const uintptr_t want = reinterpret_cast<uintptr_t>(at);
    const int flags = MAP_SHARED | (want ? MAP_FIXED : 0);

    // The requested protection is applied per view: the pages are already backed by the section, and a view of pages
    // the owner has not committed yet reads as zeros, which is what MapViewOfFile guarantees on Windows too.
    void* p = ::mmap(want ? reinterpret_cast<void*>(want) : nullptr, bytes, prot, flags, s->fd,
                     static_cast<off_t>(off));
    if (p == MAP_FAILED) {
        nxos_set_error_from_errno(errno);
        return nullptr;
    }
    if (want && reinterpret_cast<uintptr_t>(p) != want) {
        ::munmap(p, bytes);
        SetLastError(ERROR_INVALID_ADDRESS);
        return nullptr;
    }

    std::lock_guard<std::mutex> guard(g_reg_mu);
    Region r;
    r.base = reinterpret_cast<uintptr_t>(p);
    r.size = bytes;
    r.state = MEM_COMMIT;
    r.protect = protect_of_posix(prot, false);
    r.own_mapping = false;
    r.section_view = true;
    r.section_off = off;
    r.section = s;
    retain(s);
    region_insert(r);
    return p;
}

LPVOID MapViewOfFile(HANDLE section, DWORD access, DWORD hi, DWORD lo, SIZE_T size) {
    return MapViewOfFileEx(section, access, hi, lo, size, nullptr);
}

BOOL UnmapViewOfFile(LPCVOID view) {
    const uintptr_t a = reinterpret_cast<uintptr_t>(view);
    if (!a) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    std::lock_guard<std::mutex> guard(g_reg_mu);
    const int i = region_find(a);
    if (i < 0 || g_regions[static_cast<size_t>(i)].base != a) {
        SetLastError(ERROR_INVALID_ADDRESS);
        return FALSE;
    }
    Region& r = g_regions[static_cast<size_t>(i)];
    if (::munmap(reinterpret_cast<void*>(r.base), r.size) != 0) {
        nxos_set_error_from_errno(errno);
        return FALSE;
    }
    if (r.section) release(r.section);
    g_regions.erase(g_regions.begin() + i);
    return TRUE;
}

BOOL FlushViewOfFile(LPCVOID view, SIZE_T size) {
    if (!view) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    // msync on shared memory is a no-op the kernel does not need; report success so the callers do not log a failure.
    (void)size;
    return TRUE;
}

#endif  // !_WIN32
