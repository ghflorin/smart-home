/*
 * Why the last restart happened, when it was the firmware's own doing.
 *
 * Published on the vendor cluster (lock_cluster.cpp) as three attributes:
 *   0xFFF10004  FaultReason  (uint32)  Zephyr's K_ERR_*, or kNone
 *   0xFFF10005  FaultPc      (uint32)  where it happened
 *   0xFFF10006  FaultLr      (uint32)  what called it
 * The panel logs them with the restart. The addresses only mean something next
 * to the ELF of the same version: arm-zephyr-eabi-addr2line -fe zephyr.elf <pc>.
 */
#pragma once

#include <cstdint>

namespace Fault {

constexpr uint32_t kNone = 0xFFFFFFFF;

struct Info {
	uint32_t reason;
	uint32_t pc;
	uint32_t lr;
};

/* Takes over a record left by the fault handler, if the last restart was one.
 * Call first thing at boot. */
void Init(void);

const Info &Last(void);

} /* namespace Fault */
